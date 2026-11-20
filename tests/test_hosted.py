import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from server import create_server
from supportops.store import Store
from supportops.google_login import GoogleLogin
from supportops.tools import Tools


class HostedRelayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "relay.sqlite3"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.origin = f"http://127.0.0.1:{port}"
        self.server = create_server(port, store=Store(self.db), public_origin=self.origin)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, body=None, cookie=None, origin=None, include_headers=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Cookie": cookie} if cookie else {}
        if body is not None:
            headers.update(Origin=origin or self.origin, **{"Content-Type": "application/json"})
        try:
            connection.request("GET" if body is None else "POST", path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            result = response.read().decode()
            return response.status, result, response.headers if include_headers else response.getheader("Set-Cookie")
        finally:
            connection.close()

    def register(self, username):
        status, _, cookie = self.request("/api/session/register", {"username": username, "password": "test passphrase 123"})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        return cookie.split(";", 1)[0]

    def test_google_state_is_browser_bound_and_single_use(self):
        self.server.google_login = GoogleLogin("test-client.apps.googleusercontent.com", "test-secret", self.origin)
        existing = self.register("alex")
        existing_id = json.loads(self.request("/api/session", cookie=existing)[1])["user"]["id"]

        def start():
            status, raw, cookie = self.request("/api/session/google/start", {})
            self.assertEqual(status, 200)
            url = urlsplit(json.loads(raw)["url"])
            self.assertEqual(url.netloc, "accounts.google.com")
            params = parse_qs(url.query)
            self.assertEqual(params["redirect_uri"], [self.origin + "/api/session/google/callback"])
            self.assertEqual(params["scope"], ["openid profile"])
            self.assertEqual(params["code_challenge_method"], ["S256"])
            self.assertIn("SameSite=Lax", cookie)
            self.assertIn("HttpOnly", cookie)
            callback = "/api/session/google/callback?" + urlencode({"state": params["state"][0], "code": "test-code"})
            return callback, cookie.split(";", 1)[0]

        def callback_user(url, cookie):
            status, _, headers = self.request(url, cookie=cookie, include_headers=True)
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/")
            cookies = headers.get_all("Set-Cookie")
            self.assertEqual(len(cookies), 2)
            self.assertIn("Max-Age=0", cookies[0])
            session = cookies[1].split(";", 1)[0]
            return json.loads(self.request("/api/session", cookie=session)[1])["user"]

        with patch.object(self.server.google_login, "exchange", return_value=("google-subject", "alex")) as exchange:
            url, cookie = start()
            status, _, headers = self.request(url, cookie="relay_google_state=wrong", include_headers=True)
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/?signin=google-failed")
            self.assertIsNone(headers["Set-Cookie"])
            exchange.assert_not_called()

            google_user = callback_user(url, cookie)
            self.assertNotEqual(google_user["id"], existing_id)
            self.assertEqual(google_user["username"], "alex")

            status, _, headers = self.request(url, cookie=cookie, include_headers=True)
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/?signin=google-failed")
            exchange.assert_called_once()
            url, cookie = start()
            self.assertEqual(callback_user(url, cookie)["id"], google_user["id"])

    def test_google_start_rejects_cross_origin(self):
        self.server.google_login = GoogleLogin("test-client.apps.googleusercontent.com", "test-secret", self.origin)
        self.assertEqual(self.request("/api/session/google/start", {}, origin="https://other.invalid")[0], 403)

    def test_matching_workspace_policy_is_required_by_verification(self):
        policy = {"id": "POL-billing", "title": "Duplicate charge", "text": "Ask for a purchase order reference."}
        tools = Tools("acct_demo", workspace_policies=[policy], issue="Investigate a duplicate charge")
        tools.call("update_plan", {"objective": "Investigate", "steps": ["Gather evidence"], "missing_information": []})
        self.assertIn(policy["id"], {doc["id"] for doc in tools.search("Duplicate payment investigation")})
        tools.call("lookup_account", {})
        proposal = {"summary": "A billing specialist should review this charge.", "reason": "Review the ledger.",
                    "action": "billing_review", "citations": ["KB-101", "ACCOUNT"]}
        self.assertFalse(tools.verify(proposal)["passed"])
        proposal["citations"].append(policy["id"])
        self.assertTrue(tools.verify(proposal)["passed"])

    def test_private_policy_uploads_are_snapshotted_and_cited(self):
        alice, bob = self.register("alice"), self.register("bob")
        path = "/api/policies"
        self.assertEqual(self.request(path)[0], 401)
        self.assertEqual(self.request(path, {"title": "Invalid", "text": "X" * 3001}, alice)[0], 400)
        _, first, _ = self.request(path, {"title": "Duplicate charge policy", "text": "Ask for a purchase order reference for a duplicate charge."}, alice)
        _, second, _ = self.request(path, {"title": "Duplicate charge policy", "text": "Ask for the billing month for a duplicate charge."}, bob)
        first, second = json.loads(first), json.loads(second)
        self.assertEqual(json.loads(self.request(path, cookie=alice)[1])["items"], [first])
        self.assertEqual(json.loads(self.request(path, cookie=bob)[1])["items"], [second])
        self.assertEqual(self.request(path + "/remove", {"id": first["id"]}, bob)[0], 404)

        body = {"message": "Investigate a duplicate charge", "use_policies": True}
        expected = []
        for cookie, own, foreign in ((alice, first, second), (bob, second, first)):
            status, payload, _ = self.request("/api/runs", body, cookie)
            self.assertEqual(status, 202)
            run = json.loads(payload)
            self.assertEqual(run["policy_count"], 1)
            self.assertNotIn(own["text"], payload)
            expected.append((cookie, run["id"], own, foreign))
        self.assertEqual(self.request(path + "/remove", {"id": first["id"]}, alice)[0], 200)
        for cookie, run_id, own, foreign in expected:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                run = json.loads(self.request("/api/runs/" + run_id, cookie=cookie)[1])
                if run["status"] == "awaiting_approval":
                    break
                time.sleep(.05)
            self.assertEqual(run["status"], "awaiting_approval", run.get("error"))
            self.assertIn(own["id"], run["answer"]["citations"])
            self.assertNotIn(foreign["id"], json.dumps(run))
            self.assertEqual(self.server.store.run_policies(run_id), [own])

        status, payload, _ = self.request("/api/runs", body, alice)
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(payload)["policy_count"], 0)

    def test_hosted_history_and_every_run_route_are_user_scoped(self):
        self.assertEqual(self.request("/api/runs")[0], 401)
        self.assertEqual(self.request("/api/runs", {})[0], 401)
        alice, bob = self.register("alice"), self.register("bob")
        body = {"message": "Invoice charged twice", "account_id": "acct_demo", "request_id": "same-key"}
        status, reply, _ = self.request("/api/runs", body, alice)
        self.assertEqual(status, 202)
        run = json.loads(reply)
        path = "/api/runs/" + run["id"]
        self.assertEqual(json.loads(self.request("/api/runs", cookie=bob)[1])["items"], [])
        self.assertEqual(self.request("/api/runs?before=" + run["id"], cookie=bob)[0], 404)
        for suffix in ("", "/events"):
            self.assertEqual(self.request(path + suffix, cookie=bob)[0], 404)
        for suffix in ("/cancel", "/resume", "/retry", "/decision", "/ticket/retry"):
            self.assertEqual(self.request(path + suffix, {"decision": "approve"}, bob)[0], 404)
        self.assertEqual(self.request(path + "/decision", {"decision": "approve"}, alice, "https://other.invalid")[0], 403)
        status, reply, _ = self.request("/api/runs", body, bob)
        self.assertEqual(status, 202)
        bob_id = json.loads(reply)["id"]
        self.assertNotEqual(bob_id, run["id"])
        self.assertEqual(self.request(f"/api/runs/{bob_id}/{run['id']}/cancel", {}, bob)[0], 404)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = json.loads(self.request(path, cookie=alice)[1])
            if snapshot["status"] == "awaiting_approval":
                break
            time.sleep(.05)
        self.assertEqual(snapshot["status"], "awaiting_approval")
        status, reply, _ = self.request(path + "/decision", {"decision": "approve"}, alice)
        self.assertEqual(status, 200)
        ticket_id = json.loads(reply)["ticket_id"]
        self.assertTrue(ticket_id)
        self.assertIn(ticket_id, self.request(path + "/decision", {"decision": "approve"}, alice)[1])

        self.assertEqual(self.request("/api/session/logout", {}, alice)[0], 200)
        self.assertEqual(self.request(path, cookie=alice)[0], 401)
        status, _, cookie = self.request("/api/session/login", {"username": "Alice", "password": "test passphrase 123"})
        self.assertEqual(status, 200)
        self.assertIn(ticket_id, self.request(path, cookie=cookie.split(";", 1)[0])[1])
        self.assertEqual(len(Store(self.db).page()["items"]), 2)


if __name__ == "__main__":
    unittest.main()
