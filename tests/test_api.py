import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from server import create_server
from supportops.store import Store


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "api.sqlite3")
        self.server = create_server(port=0, store=self.store, capacity=1, start_workers=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_async_submission_progress_and_approval(self):
        body = {"message": "API timeout", "request_id": "same-key"}
        status, run = self.request("/api/runs", body)
        self.assertEqual(status, 202)
        self.assertEqual(run["status"], "queued")
        self.assertEqual(self.request("/api/runs", body)[1]["id"], run["id"])
        self.assertEqual(self.request(f'/api/runs/{run["id"]}/decision', {"decision": "approve"})[0], 400)
        self.server.jobs.run_one()
        self.assertEqual(self.request(f'/api/runs/{run["id"]}')[1]["status"], "awaiting_approval")
        approved = self.request(f'/api/runs/{run["id"]}/decision', {"decision": "approve"})
        self.assertEqual(approved[0], 200)
        self.assertIn("ticket_id", approved[1])
        history = self.request("/api/runs?limit=1")[1]
        self.assertEqual(history["items"][0]["status"], "approved")
        self.assertNotIn("trace", history["items"][0])

    def test_queue_full_and_invalid_input(self):
        self.assertEqual(self.request("/api/runs", {"message": "API timeout", "request_id": "one"})[0], 202)
        self.assertEqual(self.request("/api/runs", {"message": "API timeout", "request_id": "two"})[0], 429)
        self.assertEqual(self.request("/api/runs", {"message": ""})[0], 400)
        self.assertEqual(self.request("/api/runs?limit=1000")[0], 400)

    def test_foreign_origin_and_host_rejected(self):
        self.assertEqual(self.request("/api/runs", {"message": "help"}, {"Origin": "https://foreign.invalid"})[0], 403)
        self.assertEqual(self.request("/api/config", headers={"Host": "foreign.invalid"})[0], 400)

    def test_health_contains_counts_without_customer_data(self):
        status, result = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(result["queue_capacity"], 1)
        self.assertNotIn("message", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
