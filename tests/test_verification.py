"""Adversarial contract tests for cited-evidence consistency."""
import unittest
from supportops.tools import Tools, ToolError
from test_agent import FakePlanner, PLAN
from supportops.agent import investigate


class VerificationTests(unittest.TestCase):
    def prepared(self, account="acct_demo"):
        tools = Tools(account)
        tools.call("update_plan", PLAN)
        # Real fixtures; retrieving every document does not make every document cited.
        for doc in tools.docs:
            tools.call("search_knowledge", {"query": doc["title"]})
        tools.call("lookup_account", {})
        tools.call("check_service", {"service": "api"})
        tools.call("check_service", {"service": "exports"})
        return tools

    def proposal(self, text, refs=("KB-101", "ACCOUNT"), reason="Human review required"):
        return {"summary": text, "citations": list(refs), "action": "support_review", "reason": reason}

    def rejected(self, tools, proposal, code):
        result = tools.call("verify_case", proposal)
        self.assertFalse(result["passed"], proposal)
        self.assertTrue(any(item["code"] == code for item in result.get("findings", [])), result)
        with self.assertRaises(ToolError):
            tools.call("complete_case", proposal)

    def test_identifier_requires_cited_evidence_even_if_retrieved(self):
        self.rejected(self.prepared(), self.proposal("Incident INC-042 is active.", ("KB-106",)), "identifier")

    def test_identifiers_in_review_reason_are_checked(self):
        self.rejected(self.prepared(), self.proposal("Review required", reason="Refund INV-999"), "identifier")

    def test_invoice_amount_currency_and_count_cannot_be_invented(self):
        for text in ("The ledger shows USD 500 against INV-204.", "The ledger shows EUR 49 against INV-204.", "The ledger shows three settled payments against INV-204."):
            with self.subTest(text=text):
                self.rejected(self.prepared(), self.proposal(text), "payment")
        self.rejected(self.prepared("acct_basic"), self.proposal("The ledger confirms duplicate settled payments."), "payment")

    def test_account_facts_cannot_be_contradicted(self):
        cases = [("acct_basic", "Your plan is Pro.", "account_plan"),
                 ("acct_basic", "Your plan supports exports.", "export_eligibility"),
                 ("acct_locked", "The account is not locked.", "account_locked"),
                 ("acct_locked", "The account is NOT locked.", "account_locked"),
                 ("acct_demo", "The account is locked.", "account_locked")]
        for account, text, code in cases:
            with self.subTest(text=text):
                self.rejected(self.prepared(account), self.proposal(text, ("KB-104", "ACCOUNT", "STATUS-exports")), code)

    def test_service_state_and_incident_region_require_matching_source(self):
        cases = [("API service is operational.", ("KB-102", "STATUS-api"), "service_state"),
                 ("API service is degraded in eu-west-1 (INC-042).", ("KB-102", "STATUS-api"), "incident_region"),
                 ("API service is degraded.", ("KB-106",), "service_state")]
        for text, refs, code in cases:
            with self.subTest(text=text):
                self.rejected(self.prepared(), self.proposal(text, refs), code)

    def test_executed_actions_and_guarantees_are_blocked_in_both_fields(self):
        for text in ("Your refund was processed.", "The account has been unlocked.", "Your subscription is now cancelled.", "The export job was rerun.", "You will receive a refund.", "The API will recover in 10 minutes."):
            for field in ("summary", "reason"):
                with self.subTest(text=text, field=field):
                    proposal = self.proposal("Human review required", ("KB-102", "STATUS-api", "KB-101", "ACCOUNT"))
                    proposal[field] = text
                    self.rejected(self.prepared(), proposal, "action_policy" if "recover" not in text else "recovery_estimate")

    def test_correct_facts_and_negated_actions_pass(self):
        texts = [
            ("Total USD 98 against INV-204.", ("KB-101", "ACCOUNT")),
            ("49 USD against INV-204.", ("KB-101", "ACCOUNT")),
            ("Two settled USD 49 payments against INV-204. No refund has been issued or promised.", ("KB-101", "ACCOUNT")),
            ("API service is degraded in ap-south-1 (INC-042). No recovery estimate is available.", ("KB-102", "STATUS-api")),
            ("The account is not locked. Your plan is Pro.", ("KB-103", "ACCOUNT")),
            ("The export service is operational. Your plan supports exports.", ("KB-104", "ACCOUNT", "STATUS-exports")),
            ("I could not confirm duplicate settled payments from the ledger. No recovery time is available.", ("KB-101", "ACCOUNT")),
            ("API 503 errors need engineering review.", ("KB-102", "STATUS-api")),
            ("A refund has not been issued. The account has not been unlocked. The job was not rerun.", ("KB-106",)),
        ]
        for text, refs in texts:
            with self.subTest(text=text):
                tools = self.prepared()
                proposal = self.proposal(text, refs)
                self.assertTrue(tools.call("verify_case", proposal)["passed"], tools.verification)
                self.assertEqual(tools.call("complete_case", proposal), proposal)

    def test_negated_statement_does_not_hide_later_executed_action(self):
        self.rejected(self.prepared(), self.proposal("No refund was issued, but your account has been unlocked."), "action_policy")

    def test_money_is_bound_to_invoice_and_verification_checks_reason(self):
        tools = self.prepared()
        # Another real ledger record must not support this invoice's amount.
        tools.evidence["ACCOUNT"]["payments"].append({"invoice": "INV-300", "amount": 19, "currency": "USD", "state": "settled"})
        self.rejected(tools, self.proposal("Review needed", reason="USD 19 was paid for INV-204."), "payment")

    def test_count_and_amount_must_describe_the_same_payment_rows(self):
        tools = self.prepared()
        tools.evidence["ACCOUNT"]["payments"][1]["amount"] = 19
        self.rejected(tools, self.proposal("Two settled USD 49 payments against INV-204."), "payment")

    def test_decimal_amount_is_not_truncated_to_a_supported_value(self):
        self.rejected(self.prepared(), self.proposal("USD 49.999 against INV-204."), "payment")

    def test_explicit_duplicate_denial_is_not_supported_by_duplicate_ledger(self):
        self.rejected(self.prepared(), self.proposal("No duplicate settled payments exist."), "payment")

    def test_negation_cannot_hide_positive_duplicate_claim_in_next_clause(self):
        self.rejected(self.prepared("acct_basic"), self.proposal("I could not confirm a refund, but the ledger confirms duplicate settled payments."), "payment")

    def test_evidence_change_clears_displayed_verification_and_completion(self):
        tools = self.prepared()
        proposal = self.proposal("Review needed")
        self.assertTrue(tools.call("verify_case", proposal)["passed"])
        tools.call("lookup_account", {})
        self.assertIsNone(tools.verification)
        with self.assertRaises(ToolError):
            tools.call("complete_case", proposal)

    def test_plan_change_clears_displayed_verification(self):
        tools = self.prepared()
        self.assertTrue(tools.call("verify_case", self.proposal("Review needed"))["passed"])
        tools.call("update_plan", PLAN)
        self.assertIsNone(tools.verification)

    def test_live_agent_corrects_a_contradiction_after_feedback(self):
        bad = self.proposal("The ledger shows USD 500 against INV-204.")
        good = self.proposal("The ledger shows two settled USD 49 payments against INV-204.")
        planner = FakePlanner([("update_plan", PLAN), ("search_knowledge", {"query": "duplicate payment"}),
                               ("lookup_account", {}), ("verify_case", bad), ("complete_case", bad),
                               ("verify_case", good), ("complete_case", good)])
        run = investigate("Charged twice", "acct_demo", "live", planner)
        self.assertFalse(run["trace"][3]["result"]["passed"])
        self.assertFalse(run["trace"][4]["ok"])
        self.assertEqual(run["answer"]["summary"], good["summary"])
        self.assertTrue(run["verification"]["passed"])


if __name__ == "__main__":
    unittest.main()
