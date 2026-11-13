"""Offline live-planner fixtures test recovery, not live provider quality."""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from supportops.agent import investigate
from supportops.store import Store, LeaseLost, QueueFull
from supportops.jobs import JobRunner
from supportops.tools import Tools
from test_agent import FakePlanner, PLAN, BILLING

ENV = {"AGENT_PROVIDER": "gemini", "GEMINI_API_KEY": "test-only-key", "GEMINI_MODEL": "test-model", "RELAY_RETRIEVAL_MODE": "bm25"}


class Crash(BaseException):
    pass


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, ENV)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "resume.sqlite3")

    def checkpoint_after(self, calls, stop_after):
        captured = []
        def save(public, private):
            captured.append((copy.deepcopy(public), copy.deepcopy(private)))
            if len(public["trace"]) == stop_after:
                raise Crash()
        with self.assertRaises(Crash):
            investigate("Charged twice", "acct_demo", "live", FakePlanner(calls), on_checkpoint=save)
        return captured[-1]

    def test_resume_preserves_history_and_does_not_repeat_completed_tools(self):
        calls = [("update_plan", PLAN), ("search_knowledge", {"query": "duplicate payment"}), ("lookup_account", {})]
        public, state = self.checkpoint_after(calls, 3)
        planner = FakePlanner([("verify_case", BILLING), ("complete_case", BILLING)])
        original = Tools.call
        executed = []
        def call(tools, name, args):
            executed.append(name)
            return original(tools, name, args)
        with patch.object(Tools, "call", call):
            result = investigate("Charged twice", "acct_demo", "live", planner, resume_state=state)
        self.assertEqual(executed, ["verify_case", "complete_case"])
        self.assertEqual(len(result["trace"]), 5)
        self.assertEqual(result["usage"], {"input_tokens": 50, "output_tokens": 25})
        self.assertEqual(planner.histories[0][-1]["type"], "function_call_output")
        self.assertEqual(json.loads(planner.histories[0][-1]["output"])["id"], "ACCOUNT")
        self.assertNotIn("history", result)
        self.assertNotIn("test-only-key", json.dumps(state))

    def test_restored_verification_allows_only_the_exact_proposal(self):
        calls = [("update_plan", PLAN), ("search_knowledge", {"query": "duplicate payment"}), ("lookup_account", {}), ("verify_case", BILLING)]
        _, state = self.checkpoint_after(calls, 4)
        changed = {**BILLING, "summary": "A changed proposal"}
        planner = FakePlanner([("complete_case", changed), ("complete_case", BILLING)])
        result = investigate("Charged twice", "acct_demo", "live", planner, resume_state=state)
        self.assertFalse(result["trace"][-2]["ok"])
        self.assertEqual(result["answer"], BILLING)

    def test_saved_completion_is_recovered_without_a_model_call(self):
        calls = [("update_plan", PLAN), ("search_knowledge", {"query": "duplicate payment"}), ("lookup_account", {}), ("verify_case", BILLING), ("complete_case", BILLING)]
        _, state = self.checkpoint_after(calls, 5)
        planner = FakePlanner([])
        result = investigate("Charged twice", "acct_demo", "live", planner, resume_state=state)
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(result["answer"], BILLING)
        self.assertEqual(planner.histories, [])
        self.assertNotIn("ticket_id", result)

    def test_request_model_and_fixture_changes_reject_before_provider_call(self):
        _, state = self.checkpoint_after([("update_plan", PLAN)], 1)
        for message, account in [("Different input", "acct_demo"), ("Charged twice", "acct_basic")]:
            with self.subTest(message=message), self.assertRaises(ValueError):
                investigate(message, account, "live", FakePlanner([]), resume_state=state)
        planner = FakePlanner([])
        planner.model = "different-model"
        with self.assertRaises(ValueError):
            investigate("Charged twice", "acct_demo", "live", planner, resume_state=state)
        with patch.dict(os.environ, {"RELAY_RETRIEVAL_MODE": "hybrid"}):
            with self.assertRaises(ValueError):
                investigate("Charged twice", "acct_demo", "live", FakePlanner([]), resume_state=state)

    def test_total_tool_budget_is_not_reset_by_resume(self):
        _, state = self.checkpoint_after([("lookup_account", {})] * 15, 15)
        planner = FakePlanner([("lookup_account", {})])
        with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
            investigate("Charged twice", "acct_demo", "live", planner, resume_state=state)
        self.assertEqual(len(planner.histories), 1)

    def test_elapsed_budget_and_ambiguous_inflight_time_carry_over(self):
        _, state = self.checkpoint_after([("update_plan", PLAN)], 1)
        for elapsed, inflight in [(180000, None), (0, time.time() - 181)]:
            checkpoint = copy.deepcopy(state)
            checkpoint["elapsed_ms"] = elapsed
            checkpoint["inflight_started_at"] = inflight
            planner = FakePlanner([])
            with self.assertRaisesRegex(RuntimeError, "time budget"):
                investigate("Charged twice", "acct_demo", "live", planner, resume_state=checkpoint)
            self.assertEqual(planner.histories, [])

    def test_model_request_reservation_survives_crash_before_reply(self):
        captured = []
        class NoReply(FakePlanner):
            def respond(self, history):
                raise Crash()
        with self.assertRaises(Crash):
            investigate("Charged twice", "acct_demo", "live", NoReply([]),
                        on_checkpoint=lambda public, private: captured.append(copy.deepcopy(private)))
        self.assertEqual(captured[-1]["model_requests"], 1)
        self.assertIsNotNone(captured[-1]["inflight_started_at"])
        self.assertEqual(captured[-1]["trace"], [])

    def test_provider_prompt_and_fixture_changes_reject_resume(self):
        _, state = self.checkpoint_after([("update_plan", PLAN)], 1)
        with patch.dict(os.environ, {"AGENT_PROVIDER": "openai"}):
            with self.assertRaises(ValueError):
                investigate("Charged twice", "acct_demo", "live", FakePlanner([]), resume_state=state)
        with patch("supportops.agent.PROMPT", "A changed instruction"):
            with self.assertRaises(ValueError):
                investigate("Charged twice", "acct_demo", "live", FakePlanner([]), resume_state=state)
        from supportops.checkpoints import make_binding
        import supportops.agent as agent
        tools = Tools("acct_demo")
        tools.services["api"]["state"] = "operational"
        changed = make_binding("Charged twice", "acct_demo", "gemini", "test-model", tools, agent.PROMPT)
        from supportops.checkpoints import validate_state
        with self.assertRaises(ValueError):
            validate_state(state, changed)

    def test_opaque_gemini_message_is_restored_exactly_and_kept_private(self):
        import io
        from supportops.agent import GeminiPlanner
        raw = {"role": "assistant", "content": None, "tool_calls": [{"id": "opaque-call", "type": "function", "function": {"name": "update_plan", "arguments": json.dumps(PLAN)}, "extra_content": {"google": {"thought_signature": "opaque-private-signature-marker"}}}]}
        requests = []
        def response(request, timeout):
            requests.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"choices": [{"message": raw}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode())
        captured = []
        def save(public, private):
            if len(public["trace"]) == 1:
                captured.append((copy.deepcopy(public), json.loads(json.dumps(private))))
                raise Crash()
        with patch("urllib.request.urlopen", side_effect=response):
            with self.assertRaises(Crash):
                investigate("Charged twice", "acct_demo", "live", GeminiPlanner(), on_checkpoint=save)
            with self.assertRaises(Crash):
                investigate("Charged twice", "acct_demo", "live", GeminiPlanner(), resume_state=captured[0][1], on_checkpoint=lambda public, private: (_ for _ in ()).throw(Crash()) if len(public["trace"]) == 2 else None)
        self.assertEqual(requests[1]["messages"][2], raw)
        self.assertEqual(requests[1]["messages"][3]["tool_call_id"], "opaque-call")
        self.assertNotIn("opaque-private-signature-marker", json.dumps(captured[0][0]))
        self.assertNotIn("test-only-key", json.dumps(captured))

    def test_same_process_old_attempt_cannot_overwrite_resumed_work(self):
        old_wait, old_release = threading.Event(), threading.Event()
        new_wait, new_release = threading.Event(), threading.Event()
        old = FakePlanner([("update_plan", PLAN), ("lookup_account", {})])
        new = FakePlanner([("search_knowledge", {"query": "duplicate payment"}), ("lookup_account", {}), ("verify_case", BILLING), ("complete_case", BILLING)])
        def block_second(planner, reached, release):
            respond = planner.respond
            def wrapped(history):
                if len(planner.histories) == 1:
                    reached.set()
                    if not release.wait(5):
                        raise RuntimeError("Test gate timed out")
                return respond(history)
            planner.respond = wrapped
        block_second(old, old_wait, old_release)
        block_second(new, new_wait, new_release)
        self.addCleanup(old_release.set)
        self.addCleanup(new_release.set)
        runner = JobRunner(self.store)
        queued = runner.submit({"message": "Charged twice", "account_id": "acct_demo", "mode": "live"}, "race")
        with patch("supportops.agent.GeminiPlanner", side_effect=[old, new]):
            first = threading.Thread(target=runner.run_one)
            first.start()
            self.addCleanup(lambda: first.join(6))
            self.assertTrue(old_wait.wait(3))
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET lease_until=0 WHERE run_id=?", (queued["id"],))
            self.store.claim("sweeper", provider="gemini")
            runner.resume(queued["id"], "resume-race")
            second = threading.Thread(target=runner.run_one)
            second.start()
            self.addCleanup(lambda: second.join(6))
            self.assertTrue(new_wait.wait(3))
            before = self.store.get(queued["id"])
            old_release.set()
            first.join(3)
            self.assertFalse(first.is_alive())
            after = self.store.get(queued["id"])
            self.assertEqual(after["version"], before["version"])
            self.assertEqual(after["trace"][-1]["tool"], "search_knowledge")
            self.assertEqual(after["status"], "running")
            new_release.set()
            second.join(3)
        self.assertEqual(self.store.get(queued["id"])["status"], "awaiting_approval")
        self.assertEqual(self.store.metrics()["tickets"], 0)

    def test_concurrent_duplicate_resume_returns_the_existing_receipt(self):
        public, private = self.checkpoint_after([("update_plan", PLAN)], 1)
        queued = self.store.enqueue({"message": "Charged twice", "account_id": "acct_demo", "mode": "live", "provider": "gemini"}, "one")
        self.store.claim("worker", provider="gemini")
        self.store.checkpoint(queued["id"], "worker", public, private_state=private)
        self.store.checkpoint(queued["id"], "worker", {"status": "failed"}, terminal=True)
        runner = JobRunner(self.store)
        missed, queued_first = threading.Event(), threading.Event()
        receipt = self.store.resume_receipt
        result = {}
        def delayed_receipt(run_id, key):
            existing = receipt(run_id, key)
            if threading.current_thread().name == "late-resume" and existing is None:
                missed.set()
                queued_first.wait(3)
            return existing
        def late():
            try:
                result["run"] = runner.resume(queued["id"], "same-resume")
            except Exception as exc:
                result["error"] = str(exc)
        with patch.object(self.store, "resume_receipt", side_effect=delayed_receipt):
            thread = threading.Thread(target=late, name="late-resume")
            thread.start()
            self.assertTrue(missed.wait(3))
            first = runner.resume(queued["id"], "same-resume")
            queued_first.set()
            thread.join(3)
        self.assertNotIn("error", result)
        self.assertEqual(result["run"]["id"], first["id"])
        self.assertEqual(result["run"]["resume_count"], 1)

    def test_private_checkpoint_and_public_snapshot_commit_atomically(self):
        queued = self.store.enqueue({"message": "Charged twice", "account_id": "acct_demo", "mode": "live", "provider": "gemini"}, "one")
        self.store.claim("owner", provider="gemini")
        public, private = self.checkpoint_after([("update_plan", PLAN)], 1)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_checkpoint BEFORE INSERT ON agent_checkpoints BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.checkpoint(queued["id"], "owner", public, private_state=private)
        self.assertEqual(self.store.get(queued["id"])["trace"], [])
        self.assertIsNone(self.store.load_agent_checkpoint(queued["id"], "owner"))

    def test_worker_resume_survives_store_reopen_and_is_idempotent(self):
        first = FakePlanner([("update_plan", PLAN), ("search_knowledge", {"query": "duplicate payment"}), ("lookup_account", {})])
        original = first.respond
        def fail_after_three(history):
            if len(first.histories) == 3:
                raise RuntimeError("Simulated connection loss")
            return original(history)
        first.respond = fail_after_three
        runner = JobRunner(self.store)
        queued = runner.submit({"message": "Charged twice", "account_id": "acct_demo", "mode": "live"}, "one")
        with patch("supportops.agent.GeminiPlanner", return_value=first):
            runner.run_one()
        failed = self.store.get(queued["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["resume_available"])
        self.assertNotIn("_gemini_message", json.dumps(failed))
        reopened = Store(self.store.path)
        resumed_runner = JobRunner(reopened)
        resumed = resumed_runner.resume(queued["id"], "resume-one")
        self.assertEqual(resumed["id"], queued["id"])
        self.assertEqual(resumed_runner.resume(queued["id"], "resume-one")["id"], queued["id"])
        second = FakePlanner([("verify_case", BILLING), ("complete_case", BILLING)])
        with patch("supportops.agent.GeminiPlanner", return_value=second):
            resumed_runner.run_one()
        finished = reopened.get(queued["id"])
        self.assertEqual(finished["status"], "awaiting_approval")
        self.assertEqual(finished["resume_count"], 1)
        self.assertEqual(finished["model_requests"], 6)
        self.assertEqual(len(finished["trace"]), 5)
        self.assertEqual(reopened.metrics()["tickets"], 0)
        self.assertEqual(resumed_runner.resume(queued["id"], "resume-one")["status"], "awaiting_approval")
        with self.assertRaises(ValueError):
            resumed_runner.resume(queued["id"], "new-request")


if __name__ == "__main__":
    unittest.main()
