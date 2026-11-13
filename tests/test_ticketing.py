"""Approved-ticket outbox and real loopback HTTP delivery contracts."""
import json
import sqlite3
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from supportops.agent import investigate
from supportops.store import Store, LeaseLost
from supportops.ticketing import TicketConnector, DeliveryError, validate_destination
from supportops.deliveries import DeliveryRunner
from supportops.ticket_sandbox import SandboxStore, create_ticket_server

TOKEN = "test-only-sandbox-token-not-a-credential"


class TicketDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = Store(root / "relay.sqlite3")
        self.remote = SandboxStore(root / "sandbox.sqlite3")
        self.server = create_ticket_server(self.remote, TOKEN, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.target = f"http://127.0.0.1:{self.server.server_port}"
        self.connector = TicketConnector(self.target, TOKEN)
        self.runner = DeliveryRunner(self.store, self.connector)
        self.run = self.store.save(investigate("Duplicate charge", "acct_demo"))

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def approve(self):
        return self.store.decide(self.run["id"], "approve", delivery_target=self.target)

    def test_only_explicit_approval_enqueues_one_delivery(self):
        self.assertFalse(self.runner.run_one())
        self.assertEqual(self.remote.count(), 0)
        approved = self.approve()
        repeated = self.approve()
        self.assertEqual(approved["ticket_id"], repeated["ticket_id"])
        self.assertEqual(approved["ticket_delivery"]["state"], "pending")
        self.assertTrue(self.runner.run_one())
        delivered = self.store.get(self.run["id"])
        self.assertEqual(delivered["ticket_delivery"]["state"], "delivered")
        self.assertEqual(self.remote.count(), 1)
        self.assertGreater(delivered["version"], approved["version"])
        self.assertFalse(self.runner.run_one())
        self.assertNotIn(TOKEN, json.dumps(delivered))

    def test_reject_and_local_only_approval_never_send(self):
        self.store.decide(self.run["id"], "reject", delivery_target=self.target)
        self.assertFalse(self.runner.run_one())
        another = self.store.save(investigate("API timeout", "acct_demo"))
        self.store.decide(another["id"], "approve")
        self.assertNotIn("ticket_delivery", self.store.get(another["id"]))
        self.assertFalse(self.runner.run_one())
        # Enabling a connector later does not backfill an old approval.
        self.store.decide(another["id"], "approve", delivery_target=self.target)
        self.assertFalse(self.runner.run_one())
        self.assertEqual(self.remote.count(), 0)

    def test_concurrent_approvals_and_claims_are_unique(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.approve()["ticket_id"], range(4)))
        self.assertEqual(len(set(results)), 1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.runner.run_one(), range(4)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.remote.count(), 1)

    def test_outbox_insert_failure_rolls_back_local_approval(self):
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER stop_delivery BEFORE INSERT ON ticket_deliveries BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.approve()
        self.assertEqual(self.store.get(self.run["id"])["status"], "awaiting_approval")
        self.assertEqual(self.store.metrics()["tickets"], 0)

    def test_lost_acknowledgment_replay_creates_only_one_remote_ticket(self):
        self.approve()
        claimed = self.store.claim_delivery("crashed-worker", self.target)
        external = self.connector.send(claimed["payload"], claimed["idempotency_key"])
        self.assertEqual(self.remote.count(), 1)
        with self.store.connect() as db:
            db.execute("UPDATE ticket_deliveries SET lease_until=0 WHERE ticket_id=?", (claimed["ticket_id"],))
        reopened = Store(self.store.path)
        DeliveryRunner(reopened, self.connector).run_one()
        delivery = reopened.get(self.run["id"])["ticket_delivery"]
        self.assertEqual(delivery["external_id"], external)
        self.assertEqual(delivery["state"], "delivered")
        self.assertEqual(delivery["attempts"], 2)
        self.assertEqual(self.remote.count(), 1)
        with self.assertRaises(LeaseLost):
            self.store.finish_delivery(claimed["ticket_id"], "crashed-worker", external_id=external)

    def test_destination_change_does_not_redirect_pending_delivery(self):
        self.approve()
        self.assertIsNone(self.store.claim_delivery("other", "http://127.0.0.1:65534"))
        self.assertEqual(self.store.get(self.run["id"])["ticket_delivery"]["state"], "pending")

    def test_transient_error_is_retried_then_exhausted_with_manual_recovery(self):
        self.approve()
        with patch.object(self.connector, "send", side_effect=DeliveryError("temporary", retryable=True)):
            for _ in range(5):
                self.assertTrue(self.runner.run_one())
                with self.store.connect() as db:
                    db.execute("UPDATE ticket_deliveries SET next_attempt=0")
        failed = self.store.get(self.run["id"])["ticket_delivery"]
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["attempts"], 5)
        self.assertEqual(self.remote.count(), 0)
        self.store.retry_delivery(self.run["id"], self.target)
        self.runner.run_one()
        self.assertEqual(self.store.get(self.run["id"])["ticket_delivery"]["attempts"], 6)
        self.assertEqual(self.remote.count(), 1)

    def test_unicode_payload_with_maximum_text_lengths_is_delivered(self):
        self.approve()
        claimed = self.store.claim_delivery("worker", self.target)
        payload = {**claimed["payload"], "summary": "界" * 4000, "reason": "😀" * 4000}
        external = self.connector.send(payload, claimed["idempotency_key"])
        self.assertTrue(external.startswith("SBX-"))
        self.assertEqual(self.remote.count(), 1)

    def test_run_version_and_delivery_state_are_a_single_read_snapshot(self):
        self.approve()
        claimed = self.store.claim_delivery("worker", self.target)
        before = self.store.get(self.run["id"])
        writer = Store(self.store.path)
        connect = self.store.connect
        class Cursor:
            def __init__(self, cursor, after_read):
                self.cursor, self.after_read = cursor, after_read
            def fetchone(self):
                result = self.cursor.fetchone()
                self.cursor.close()
                self.after_read()
                return result
        class Interleave:
            triggered = False
            def __init__(self, db):
                self.db = db
            def after_read(self):
                if not self.triggered:
                    self.triggered = True
                    writer.finish_delivery(claimed["ticket_id"], "worker", external_id="SBX-AAAAAAAAAAAA")
            def execute(self, sql, params=()):
                cursor = self.db.execute(sql, params)
                return Cursor(cursor, self.after_read) if sql.lstrip().upper().startswith("SELECT") else cursor
        @contextmanager
        def interleaved_connection():
            with connect() as db:
                yield Interleave(db)
        with patch.object(self.store, "connect", side_effect=interleaved_connection):
            observed = self.store.get(self.run["id"])
        valid = {(before["version"], "sending"), (before["version"] + 1, "delivered")}
        self.assertIn((observed["version"], observed["ticket_delivery"]["state"]), valid)

    def test_fifth_crashed_attempt_becomes_failed_without_sixth_send(self):
        self.approve()
        claimed = self.store.claim_delivery("crashed", self.target)
        with self.store.connect() as db:
            db.execute("UPDATE ticket_deliveries SET cycle_attempts=5,attempts=5,lease_until=0 WHERE ticket_id=?", (claimed["ticket_id"],))
        self.assertFalse(self.runner.run_one())
        delivery = self.store.get(self.run["id"])["ticket_delivery"]
        self.assertEqual(delivery["state"], "failed")
        self.assertEqual(delivery["attempts"], 5)
        self.assertEqual(self.remote.count(), 0)

    def test_stale_worker_cannot_overwrite_a_new_claim(self):
        self.approve()
        first = self.store.claim_delivery("old", self.target)
        with self.store.connect() as db:
            db.execute("UPDATE ticket_deliveries SET lease_until=0 WHERE ticket_id=?", (first["ticket_id"],))
        second = self.store.claim_delivery("new", self.target)
        with self.assertRaises(LeaseLost):
            self.store.finish_delivery(first["ticket_id"], "old", external_id="SBX-AAAAAAAAAAAA")
        self.assertEqual(self.store.get(self.run["id"])["ticket_delivery"]["state"], "sending")
        self.store.finish_delivery(second["ticket_id"], "new", external_id=self.connector.send(second["payload"], second["idempotency_key"]))
        self.assertEqual(self.remote.count(), 1)

    def test_manual_retry_cannot_change_the_destination(self):
        self.approve()
        with patch.object(self.connector, "send", side_effect=DeliveryError("access")):
            self.runner.run_one()
        with self.assertRaises(ValueError):
            self.store.retry_delivery(self.run["id"], "http://127.0.0.1:65534")
        self.assertEqual(self.store.get(self.run["id"])["ticket_delivery"]["state"], "failed")

    def test_backoff_does_not_immediately_resend_a_failed_request(self):
        self.approve()
        with patch.object(self.connector, "send", side_effect=DeliveryError("quota", retryable=True, retry_after=30)):
            self.assertTrue(self.runner.run_one())
            self.assertFalse(self.runner.run_one())
        delivery = self.store.get(self.run["id"])["ticket_delivery"]
        self.assertEqual(delivery["state"], "pending")
        self.assertEqual(delivery["attempts"], 1)
        self.assertGreater(delivery["next_attempt"], time.time() + 20)

    def test_auth_error_is_terminal_and_sanitized(self):
        self.approve()
        runner = DeliveryRunner(self.store, TicketConnector(self.target, "wrong-test-token-at-least-16"))
        runner.run_one()
        result = self.store.get(self.run["id"])["ticket_delivery"]
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error_code"], "access")
        self.assertEqual(self.remote.count(), 0)

    def test_service_rejects_changed_payload_for_same_idempotency_key(self):
        self.approve()
        delivery = self.store.claim_delivery("worker", self.target)
        self.connector.send(delivery["payload"], delivery["idempotency_key"])
        with self.assertRaises(DeliveryError) as caught:
            self.connector.send({**delivery["payload"], "summary": "Changed after approval"}, delivery["idempotency_key"])
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self.remote.count(), 1)

    def test_real_http_errors_and_redirects_are_classified_without_following(self):
        self.approve()
        claimed = self.store.claim_delivery("worker", self.target)
        for status, body, code, retryable in [(429, b"private error body", "quota", True), (503, b"private error body", "temporary", True), (302, b"", "request", False), (200, b"invalid JSON", "invalid_response", True), (200, b"x" * 9000, "invalid_response", True)]:
            with self.subTest(status=status, code=code):
                def respond(handler):
                    handler.rfile.read(int(handler.headers["Content-Length"]))
                    handler.send_response(status)
                    handler.send_header("Content-Length", str(len(body)))
                    handler.send_header("Retry-After", "30")
                    handler.send_header("Location", "http://127.0.0.1:1/forbidden")
                    handler.end_headers()
                    handler.wfile.write(body)
                with patch.object(self.server.RequestHandlerClass, "do_POST", respond):
                    with self.assertRaises(DeliveryError) as caught:
                        self.connector.send(claimed["payload"], claimed["idempotency_key"])
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertNotIn("private error body", str(caught.exception))
        self.assertEqual(self.remote.count(), 0)

    def test_actual_dropped_http_response_retries_the_existing_remote_ticket(self):
        self.approve()
        def accept_then_drop(handler):
            payload = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
            handler.server.store.create(handler.headers["Idempotency-Key"], payload)
            handler.connection.shutdown(socket.SHUT_RDWR)
            handler.connection.close()
        with patch.object(self.server.RequestHandlerClass, "do_POST", accept_then_drop):
            self.runner.run_one()
        self.assertEqual(self.remote.count(), 1)
        self.assertEqual(self.store.get(self.run["id"])["ticket_delivery"]["state"], "pending")
        with self.store.connect() as db:
            db.execute("UPDATE ticket_deliveries SET next_attempt=0")
        self.runner.run_one()
        self.assertEqual(self.store.get(self.run["id"])["ticket_delivery"]["state"], "delivered")
        self.assertEqual(self.remote.count(), 1)

    def test_invalid_destination_cannot_send_credentials_elsewhere(self):
        for target in ("https://example.com", "http://localhost:8014", "http://127.0.0.1:8014/path", "http://user:pass@127.0.0.1:8014", "http://127.0.0.1:8014?token=secret"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_destination(target)


if __name__ == "__main__":
    unittest.main()
