import json
import tempfile
import unittest
from pathlib import Path
from supportops.agent import investigate, MAX_STEPS
from supportops.store import Store
from supportops.tools import Tools, ToolError

PLAN = {"objective": "Investigate", "steps": ["Retrieve evidence", "Verify proposal"], "missing_information": []}
BILLING = {"summary": "Billing review required", "citations": ["ACCOUNT", "KB-101"], "action": "billing_review", "reason": "Duplicate invoice"}
UNKNOWN = {"summary": "Human review required", "citations": ["KB-106"], "action": "support_review", "reason": "Unknown"}


class FakePlanner:
    model = "test-model"

    def __init__(self, calls):
        self.calls = iter(calls)
        self.histories = []

    def respond(self, history):
        self.histories.append(list(history))
        name, args = next(self.calls)
        return {"output": [{"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": str(len(self.histories))}], "usage": {"input_tokens": 10, "output_tokens": 5}}


class AgentTests(unittest.TestCase):
    def test_duplicate_payment_evidence(self):
        run = investigate("Duplicate charge for invoice", "acct_demo")
        self.assertEqual(run["answer"]["action"], "billing_review")
        self.assertIn("two settled", run["answer"]["summary"])
        self.assertIn("ACCOUNT", run["answer"]["citations"])
        self.assertEqual(run["status"], "awaiting_approval")
        self.assertNotIn("ticket_id", run)

    def test_no_duplicate_is_not_invented(self):
        run = investigate("Duplicate charge", "acct_basic")
        self.assertIn("could not confirm", run["answer"]["summary"])

    def test_outage_has_incident_evidence(self):
        run = investigate("API timeout 503 errors", "acct_demo")
        self.assertEqual(run["answer"]["action"], "engineering_escalation")
        self.assertIn("INC-042", run["answer"]["summary"])
        self.assertIn("STATUS-api", run["answer"]["citations"])

    def test_plan_restriction_and_lock(self):
        self.assertIn("not eligible", investigate("export failed", "acct_basic")["answer"]["summary"])
        self.assertIn("account is locked", investigate("login locked", "acct_locked")["answer"]["summary"])

    def test_unknown_abstains(self):
        run = investigate("The dashboard chart is strange", "acct_demo")
        self.assertIn("does not establish a cause", run["answer"]["summary"])

    def test_tools_enforce_boundary(self):
        tools = Tools("acct_demo")
        for name, args in [("refund", {}), ("lookup_account", {"account_id": "acct_locked"}), ("check_service", {"service": "database"}), ("search_knowledge", {"query": ""})]:
            with self.assertRaises(ToolError):
                tools.call(name, args)
        with self.assertRaises(ToolError):
            tools.call("complete_case", {"summary": "Refund issued", "citations": ["FAKE"], "action": "billing_review", "reason": "test"})

    def test_live_tool_feedback_and_usage(self):
        planner = FakePlanner([("update_plan", PLAN), ("search_knowledge", {"query": "duplicate payment"}), ("lookup_account", {}), ("verify_case", BILLING), ("complete_case", BILLING)])
        run = investigate("Charged twice", "acct_demo", "live", planner)
        self.assertEqual(run["usage"], {"input_tokens": 50, "output_tokens": 25})
        self.assertTrue(run["verification"]["passed"])
        self.assertTrue(any(x.get("type") == "function_call_output" for x in planner.histories[-1]))

    def test_live_recovers_from_invalid_tool(self):
        planner = FakePlanner([("refund", {}), ("update_plan", PLAN), ("search_knowledge", {"query": "unknown issues"}), ("verify_case", UNKNOWN), ("complete_case", UNKNOWN)])
        run = investigate("Help", "acct_demo", "live", planner)
        self.assertFalse(run["trace"][0]["ok"])
        self.assertTrue(run["trace"][-1]["ok"])

    def test_live_collects_missing_evidence_after_verification_failure(self):
        planner = FakePlanner([("update_plan", PLAN), ("search_knowledge", {"query": "duplicate payment"}), ("verify_case", BILLING), ("lookup_account", {}), ("verify_case", BILLING), ("complete_case", BILLING)])
        run = investigate("Charged twice", "acct_demo", "live", planner)
        self.assertFalse(run["trace"][2]["result"]["passed"])
        self.assertTrue(run["verification"]["passed"])
        feedback = planner.histories[3][-1]
        self.assertIn("ACCOUNT", feedback["output"])

    def test_verified_draft_cannot_be_changed_or_reused_after_new_evidence(self):
        tools = Tools("acct_demo")
        tools.call("update_plan", PLAN)
        tools.call("search_knowledge", {"query": "duplicate payment"})
        tools.call("lookup_account", {})
        self.assertTrue(tools.call("verify_case", BILLING)["passed"])
        with self.assertRaises(ToolError):
            tools.call("complete_case", {**BILLING, "summary": "A different reply"})
        tools.call("check_service", {"service": "api"})
        with self.assertRaises(ToolError):
            tools.call("complete_case", BILLING)

    def test_checker_rejects_unknown_incident_and_executed_refund(self):
        tools = Tools("acct_demo")
        tools.call("update_plan", PLAN)
        tools.call("search_knowledge", {"query": "duplicate payment"})
        tools.call("lookup_account", {})
        result = tools.call("verify_case", {**BILLING, "summary": "I have issued your refund for INV-999."})
        self.assertFalse(result["passed"])
        self.assertTrue(any("INV-999" in issue for issue in result["issues"]))
        self.assertTrue(any("executed" in issue for issue in result["issues"]))

    def test_demo_exposes_plan_revisions_and_verification(self):
        run = investigate("Data export failed", "acct_basic")
        self.assertEqual(len(run["plan_history"]), 2)
        self.assertTrue(run["verification"]["passed"])
        names = [step["tool"] for step in run["trace"]]
        self.assertEqual(names[0], "update_plan")
        self.assertEqual(names[-2:], ["verify_case", "complete_case"])

    def test_live_budget_stops_loop(self):
        planner = FakePlanner([("lookup_account", {})] * MAX_STEPS)
        with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
            investigate("Help", "acct_demo", "live", planner)

    def test_input_validation(self):
        for message, account, mode in [("", "acct_demo", "demo"), ("a"*4001, "acct_demo", "demo"), ("help", "unknown", "demo"), ("help", "acct_demo", "bogus")]:
            with self.assertRaises(ValueError):
                investigate(message, account, mode)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.run = self.store.save(investigate("API timeout", "acct_demo"))

    def tearDown(self):
        self.temp.cleanup()

    def test_approval_is_idempotent_and_durable(self):
        approved = self.store.decide(self.run["id"], "approve")
        again = self.store.decide(self.run["id"], "approve")
        self.assertEqual(approved["ticket_id"], again["ticket_id"])
        self.assertEqual(self.store.get(self.run["id"])["status"], "approved")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
        with self.assertRaises(ValueError):
            self.store.decide(self.run["id"], "reject")

    def test_rejection_creates_no_ticket(self):
        rejected = self.store.decide(self.run["id"], "reject")
        self.assertEqual(rejected["status"], "rejected")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
