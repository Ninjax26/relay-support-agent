import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from supportops.agent import investigate
from supportops.jobs import JobRunner
from supportops.store import Store, QueueFull, LeaseLost

REQUEST = {"message": "API timeout", "account_id": "acct_demo", "mode": "demo"}


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.store = Store(self.path)
        self.runner = JobRunner(self.store)

    def tearDown(self):
        self.runner.close()
        self.temp.cleanup()

    def test_duplicate_submission_and_changed_input(self):
        first = self.runner.submit(REQUEST, "request-1")
        again = self.runner.submit(REQUEST, "request-1")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(self.store.metrics()["jobs"], {"queued": 1})
        with self.assertRaises(ValueError):
            self.runner.submit({**REQUEST, "message": "login locked"}, "request-1")

    def test_capacity_and_approval_boundary(self):
        runner = JobRunner(self.store, capacity=1)
        run = runner.submit(REQUEST, "one")
        with self.assertRaises(QueueFull):
            runner.submit(REQUEST, "two")
        with self.assertRaises(ValueError):
            self.store.decide(run["id"], "approve")
        runner.run_one()
        done = self.store.get(run["id"])
        self.assertEqual(done["status"], "awaiting_approval")
        self.assertEqual(len(done["trace"]), 7)
        self.assertGreater(done["version"], 2)
        self.assertNotIn("ticket_id", done)
        ticket = self.store.decide(run["id"], "approve")["ticket_id"]
        self.assertEqual(self.store.decide(run["id"], "approve")["ticket_id"], ticket)
        runner.submit(REQUEST, "three")

    def test_only_one_worker_claims_a_job(self):
        self.runner.submit(REQUEST, "one")
        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed = list(pool.map(lambda i: self.store.claim(f"worker-{i}"), range(8)))
        self.assertEqual(sum(run is not None for run in claimed), 1)

    def test_queued_job_survives_store_reopen(self):
        queued = self.runner.submit(REQUEST, "one")
        reopened = Store(self.path)
        self.assertEqual(reopened.claim("new-worker")["id"], queued["id"])

    def test_failure_preserves_checkpoint_and_retry_is_new_run(self):
        def fail(message, account, mode, on_step):
            on_step({"trace": [{"step": 1, "tool": "update_plan", "ok": True}], "duration_ms": 1})
            raise RuntimeError("Simulated provider failure")
        self.runner.agent = fail
        run = self.runner.submit(REQUEST, "one")
        self.runner.run_one()
        failed = self.store.get(run["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(failed["trace"]), 1)
        retry = self.runner.submit(REQUEST, "retry-key", retry_of=run["id"])
        self.assertNotEqual(retry["id"], run["id"])
        self.assertEqual(retry["retry_of"], run["id"])
        self.assertEqual(self.runner.submit(REQUEST, "retry-key", retry_of=run["id"])["id"], retry["id"])
        self.runner.agent = investigate
        self.runner.run_one()
        self.assertEqual(self.store.get(retry["id"])["status"], "awaiting_approval")
        self.assertEqual(self.store.get(run["id"])["status"], "failed")

    def test_expired_worker_is_fenced_and_not_automatically_replayed(self):
        run = self.runner.submit(REQUEST, "one")
        self.store.claim("old-worker")
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET lease_until=0 WHERE run_id=?", (run["id"],))
        self.store.heartbeat("old-worker")
        self.assertIsNone(self.store.claim("new-worker"))
        self.assertEqual(self.store.get(run["id"])["status"], "interrupted")
        with self.assertRaises(LeaseLost):
            self.store.checkpoint(run["id"], "old-worker", {"status": "awaiting_approval"}, terminal=True)

    def test_complete_job_cannot_be_retried(self):
        run = self.runner.submit(REQUEST, "one")
        self.runner.run_one()
        with self.assertRaises(ValueError):
            self.runner.submit(REQUEST, "retry", retry_of=run["id"])

    def test_history_pages_are_compact_and_no_duplicate_timestamps(self):
        runs = [self.runner.submit(REQUEST, f"key-{i}") for i in range(5)]
        with self.store.connect() as db:
            db.execute("UPDATE runs SET created_at='2026-01-01T00:00:00+00:00'")
        ids, cursor = [], None
        while True:
            page = self.store.page(limit=2, before=cursor)
            ids.extend(run["id"] for run in page["items"])
            self.assertTrue(all("trace" not in run for run in page["items"]))
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(set(ids), {run["id"] for run in runs})
        self.assertEqual(len(ids), 5)


if __name__ == "__main__":
    unittest.main()
