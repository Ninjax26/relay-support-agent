import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path
from supportops.agent import investigate
from supportops.evaluation import load_cases, grade, evaluate, report_metadata, write_report, summarize_review


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.dataset, self.dev = load_cases("dev")
        self.case = next(case for case in self.dev if case["id"] == "billing-01")
        self.run = investigate(self.case["issue"], self.case["account_id"])

    def test_split_integrity_and_unique_cases(self):
        _, test = load_cases("test")
        self.assertEqual(len(self.dev), 35)
        self.assertEqual(len(test), 35)
        self.assertFalse({case["id"] for case in self.dev} & {case["id"] for case in test})
        self.assertEqual({case["category"] for case in self.dev}, {case["category"] for case in test})
        self.assertEqual(len({case["issue"] for case in self.dataset["cases"]}), 70)

    def test_correct_fixture_run_passes(self):
        self.assertTrue(grade(self.case, self.run)["passed"])

    def test_wrong_action_missing_tool_and_fabricated_citation_fail(self):
        run = copy.deepcopy(self.run)
        run["answer"]["action"] = "identity_review"
        run["trace"] = [step for step in run["trace"] if step["tool"] != "lookup_account"]
        run["answer"]["citations"] = ["INVENTED"]
        failures = grade(self.case, run)["failures"]
        for required in ["action", "tool_coverage", "citation_coverage", "citation_validity"]:
            self.assertIn(required, failures)

    def test_unapproved_write_and_unsupported_claim_fail(self):
        run = copy.deepcopy(self.run)
        run["ticket_id"] = "TKT-UNAPPROVED"
        run["answer"]["summary"] = "I have issued your refund."
        failures = grade(self.case, run)["failures"]
        self.assertIn("verification_boundary", failures)
        self.assertIn("response_text", failures)

    def test_unknown_case_requires_abstention_and_clarification(self):
        case = next(case for case in self.dev if case["category"] == "unknown")
        run = investigate(case["issue"], case["account_id"])
        self.assertTrue(grade(case, run)["passed"])
        run["answer"]["summary"] = "The problem is fixed."
        failures = grade(case, run)["failures"]
        self.assertIn("abstention_proxy", failures)
        self.assertIn("clarification_proxy", failures)

    def test_execution_errors_count_against_rates_and_keep_partial_usage(self):
        def fail(message, account, mode, on_step):
            on_step({"trace": [{"tool": "refund", "ok": False}], "usage": {"input_tokens": 7, "output_tokens": 2}})
            raise RuntimeError("Simulated provider failure")
        report = evaluate(self.dev[:4], agent=fail)
        self.assertEqual(report["attempted"], 3)
        self.assertEqual(report["planned"], 4)
        self.assertEqual(report["pass_rate"], 0)
        self.assertEqual(report["tokens"]["input_tokens"], 21)
        self.assertEqual(report["invalid_tool_attempts"], 3)
        self.assertTrue(report["stopped_early"])

    def test_quota_failure_stops_after_one_request(self):
        def fail(*args, **kwargs):
            raise RuntimeError("Gemini rate limit or quota reached")
        report = evaluate(self.dev[:3], agent=fail)
        self.assertEqual(report["attempted"], 1)

    def test_unexpected_failure_is_reported_without_raw_details(self):
        def fail(*args, **kwargs):
            raise KeyError("private provider detail")
        report = evaluate([self.case], agent=fail)
        self.assertEqual(report["completed"], 0)
        self.assertIn("KeyError", report["results"][0]["error"])
        self.assertNotIn("private provider detail", report["results"][0]["error"])

    def test_nonapplicable_proxy_rates_are_not_inflated(self):
        report = evaluate([self.case], repeats=2)
        self.assertEqual(report["attempted"], 2)
        self.assertIsNone(report["check_rates"]["abstention_proxy"])
        self.assertIsNone(report["check_rates"]["clarification_proxy"])

    def test_reports_include_reproducibility_and_unfilled_human_review(self):
        report = {**report_metadata(self.dataset, "dev", "demo", 1), **evaluate([self.case])}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            result, review = write_report(report, path, [self.case])
            stored = json.loads(result.read_text())
            self.assertEqual(len(stored["dataset_sha256"]), 64)
            self.assertEqual(len(stored["prompt_sha256"]), 64)
            self.assertIn("groundedness_correct", review.read_text())
            self.assertNotIn("GEMINI_API_KEY", result.read_text())
            with self.assertRaises(FileExistsError):
                write_report(report, path, [self.case])

    def test_human_review_blank_is_not_counted_as_correct(self):
        report = {**report_metadata(self.dataset, "dev", "demo", 1), **evaluate([self.case])}
        with tempfile.TemporaryDirectory() as directory:
            _, review = write_report(report, Path(directory) / "result.json", [self.case])
            summary = summarize_review(report, review)
            self.assertIsNone(summary["human_review"]["groundedness_correct"]["rate"])
            self.assertEqual(summary["human_review"]["groundedness_correct"]["coverage_of_completed"], 0)
            with review.open(newline="") as handle:
                reader = csv.DictReader(handle)
                rows, fields = list(reader), reader.fieldnames
            rows[0].update(groundedness_correct="yes", resolution_useful="no", abstention_appropriate="na")
            with review.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            summary = summarize_review(report, review)
            self.assertEqual(summary["human_review"]["groundedness_correct"]["rate"], 1)
            self.assertEqual(summary["human_review"]["resolution_useful"]["rate"], 0)
            self.assertIsNone(summary["human_review"]["abstention_appropriate"]["rate"])


if __name__ == "__main__":
    unittest.main()
