import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from server import create_server
from supportops.store import Store
from supportops.ticketing import TicketConnector
from supportops.ticket_sandbox import SandboxStore, create_ticket_server

TOKEN = "test-only-sandbox-token-not-a-credential"


class TicketAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.remote = SandboxStore(root / "remote.sqlite3")
        self.ticket_server = create_ticket_server(self.remote, TOKEN, port=0)
        self.ticket_thread = threading.Thread(target=self.ticket_server.serve_forever, daemon=True)
        self.ticket_thread.start()
        self.addCleanup(self.close_ticket_server)
        self.connector = TicketConnector(f"http://127.0.0.1:{self.ticket_server.server_port}", TOKEN)
        self.server = create_server(port=0, store=Store(root / "app.sqlite3"), start_workers=False, ticket_connector=self.connector)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_app)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close_ticket_server(self):
        self.ticket_server.shutdown()
        self.ticket_server.server_close()
        self.ticket_thread.join()

    def close_app(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, body=None):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_http_approval_delivers_only_approved_fields_to_sandbox(self):
        _, queued = self.request("/api/runs", {"message": "API timeout", "request_id": "one"})
        self.server.jobs.run_one()
        self.assertFalse(self.server.deliveries.run_one())
        status, approved = self.request(f'/api/runs/{queued["id"]}/decision', {"decision": "approve", "delivery_target": "https://attacker.invalid"})
        self.assertEqual(status, 200)
        self.assertEqual(approved["ticket_delivery"]["state"], "pending")
        self.server.deliveries.run_one()
        _, delivered = self.request('/api/runs/' + queued["id"])
        self.assertEqual(delivered["ticket_delivery"]["state"], "delivered")
        with self.remote.connect() as db:
            payload = json.loads(db.execute("SELECT payload FROM sandbox_tickets").fetchone()[0])
        self.assertEqual(set(payload), {"ticket_id", "run_id", "action", "summary", "reason", "citations"})
        self.assertNotIn(TOKEN, json.dumps(delivered))
        self.assertEqual(self.remote.count(), 1)

    def test_ticket_retry_is_separate_from_investigation_retry(self):
        _, queued = self.request("/api/runs", {"message": "API timeout"})
        self.server.jobs.run_one()
        self.request(f'/api/runs/{queued["id"]}/decision', {"decision": "approve"})
        wrong = TicketConnector(self.connector.destination, "incorrect-test-token-at-least-16")
        from supportops.deliveries import DeliveryRunner
        DeliveryRunner(self.server.store, wrong).run_one()
        status, retried = self.request(f'/api/runs/{queued["id"]}/ticket/retry', {})
        self.assertEqual(status, 200)
        self.assertEqual(retried["id"], queued["id"])
        self.assertEqual(retried["ticket_delivery"]["state"], "pending")
        self.server.deliveries.run_one()
        self.assertEqual(self.remote.count(), 1)
        self.assertEqual(self.request(f'/api/runs/{queued["id"]}/ticket/retry', {})[0], 400)

    def test_background_delivery_starts_and_stops_with_server(self):
        app = create_server(port=0, store=self.server.store, ticket_connector=self.connector)
        self.addCleanup(app.server_close)
        queued = app.jobs.submit({"message": "API timeout", "account_id": "acct_demo", "mode": "demo"}, "automatic")
        deadline = time.monotonic() + 5
        while app.store.get(queued["id"])["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(.02)
        approved = app.store.decide(queued["id"], "approve", delivery_target=self.connector.destination)
        while app.store.get(approved["id"])["ticket_delivery"]["state"] != "delivered" and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(app.store.get(approved["id"])["ticket_delivery"]["state"], "delivered")
        self.assertEqual(self.remote.count(), 1)
        app.server_close()
        self.assertFalse(app.deliveries.thread.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in app.jobs.threads))

    def test_config_reports_connector_without_secret_or_destination(self):
        _, config = self.request('/api/config')
        self.assertEqual(config['ticket_connector'], 'sandbox')
        self.assertNotIn(TOKEN, json.dumps(config))
        self.assertNotIn(self.connector.destination, json.dumps(config))


if __name__ == "__main__":
    unittest.main()
