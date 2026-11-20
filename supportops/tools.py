"""Read-only fixtures, lexical retrieval, and strict tool argument validation."""
import json
from pathlib import Path
from .retrieval import Retriever, configured_mode, tokens
from .verification import check_proposal, CHECKS, LIMITATION

ROOT = Path(__file__).resolve().parent.parent
POLICY_QUERY_STOP_WORDS = set(
    "a am an and are as at be but by can could do for from had has have how i if in is it me my of on or our "
    "please so that the their they this to us was we were what when where which who will with would you your".split()
)


class ToolError(ValueError):
    pass


class Tools:
    def __init__(self, account_id, retrieval_mode=None, workspace_policies=None, issue=None):
        self.account_id = account_id
        self.docs = json.loads((ROOT / "data/knowledge.json").read_text())
        self.accounts = json.loads((ROOT / "data/accounts.json").read_text())
        self.services = json.loads((ROOT / "data/services.json").read_text())
        self.retriever = Retriever(self.docs, configured_mode() if retrieval_mode is None else retrieval_mode)
        self.workspace_retriever = Retriever(workspace_policies) if workspace_policies else None
        if workspace_policies:
            self.docs.extend(workspace_policies)
        self.issue_policy, self.issue_policy_metadata = self.match_workspace_policy(issue)
        self.retrieval_events = []
        self.evidence = {}
        self.plan = None
        self.plan_history = []
        self.verification = None
        self.verified_proposal = None

    def verify(self, proposal):
        """Mechanical evidence/policy checks, not a semantic LLM truth judge."""
        refs = proposal["citations"]
        issues = []
        if not self.plan:
            issues.append("Create an investigation plan with update_plan first.")
        if not refs:
            issues.append("Provide at least one retrieved evidence citation.")
        for ref in refs:
            if ref not in self.evidence:
                issues.append(f"Retrieve evidence {ref} before citing it.")
        requirements = {
            "billing_review": {"ACCOUNT", "KB-101"},
            "engineering_escalation": {"STATUS-api", "KB-102"},
            "identity_review": {"ACCOUNT", "KB-103"},
            "support_review": set(),
        }
        action = proposal["action"]
        if action not in requirements:
            issues.append("Unsupported review action.")
        else:
            for ref in sorted(requirements[action] - set(refs)):
                issues.append(f"Retrieve and cite {ref} before proposing {action}.")
        if not any(ref.startswith("KB-") for ref in refs):
            issues.append("Retrieve and cite a knowledge playbook.")
        if self.issue_policy:
            identifier = self.issue_policy[0]["id"]
            if identifier not in refs:
                issues.append(f"Retrieve and cite workspace policy {identifier}. Search using the original customer issue, then explain the policy's applicable guidance or any conflict.")
        findings = check_proposal(proposal, {ref: self.evidence[ref] for ref in refs if ref in self.evidence})
        issues.extend(finding["message"] for finding in findings)
        summary = proposal["summary"]
        if action == "support_review" and "export" in summary.lower():
            for ref in {"ACCOUNT", "KB-104", "STATUS-exports"} - set(refs):
                issues.append(f"Retrieve and cite {ref} for an export investigation.")
        self.verification = {"passed": not issues, "issues": issues, "checks": ["plan present", "retrieved citations", "action evidence coverage", *CHECKS], "findings": findings,
                             "limitation": LIMITATION}
        self.verified_proposal = json.dumps(proposal, sort_keys=True) if not issues else None
        return self.verification

    def match_workspace_policy(self, query):
        if self.workspace_retriever and isinstance(query, str):
            terms = " ".join(term for term in tokens(query) if term not in POLICY_QUERY_STOP_WORDS)
            if terms:
                return self.workspace_retriever.search(terms, k=1)
        return [], None

    def search(self, query):
        """Retrieve cited playbooks and preserve the actual retrieval strategy."""
        selected, metadata = self.retriever.search(query)
        self.retrieval_events.append(metadata)
        if self.issue_policy and self.issue_policy[0]["id"] not in self.evidence:
            matches, workspace_metadata = self.issue_policy, self.issue_policy_metadata
        else:
            matches, workspace_metadata = self.match_workspace_policy(query)
        if workspace_metadata:
            self.retrieval_events.append({**workspace_metadata, "source": "workspace"})
        selected = matches + selected
        for doc in selected:
            self.evidence[doc["id"]] = doc
        return selected

    def call(self, name, args):
        if name not in SCHEMAS:
            raise ToolError("Unknown tool")
        schema = SCHEMAS[name]
        if not isinstance(args, dict) or set(args) != set(schema):
            raise ToolError("Tool arguments do not match the schema")
        for key, kind in schema.items():
            value = args[key]
            if kind == "strings":
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value) or len(value) > 20:
                    raise ToolError("Expected a bounded list of evidence IDs")
            elif not isinstance(value, str) or not value.strip() or len(value) > 4000:
                raise ToolError("Expected a nonempty string under 4000 characters")
        if name == "update_plan":
            if not args["steps"] or any(not step.strip() or len(step) > 500 for step in args["steps"]):
                raise ToolError("Provide 1–20 nonempty plan steps under 500 characters")
            self.plan = args
            self.plan_history.append(args)
            self.verified_proposal = None
            self.verification = None
            return {"recorded": True, "revision": len(self.plan_history), **args}
        if name in {"search_knowledge", "lookup_account", "check_service"}:
            self.verified_proposal = None
            self.verification = None
        if name == "search_knowledge":
            return self.search(args["query"])
        if name == "lookup_account":
            # Scope comes from the run, never from the model's arguments.
            account = self.accounts.get(self.account_id)
            if not account:
                return {"found": False}
            result = {"id": "ACCOUNT", **account}
            self.evidence["ACCOUNT"] = result
            return result
        if name == "check_service":
            if args["service"] not in self.services:
                raise ToolError("Only api and exports services are supported")
            result = {"id": "STATUS-" + args["service"], **self.services[args["service"]]}
            self.evidence[result["id"]] = result
            return result
        if name == "verify_case":
            return self.verify(args)
        if name == "complete_case":
            if self.verified_proposal != json.dumps(args, sort_keys=True):
                raise ToolError("Call verify_case successfully with this exact proposal after the latest evidence/plan change, then complete_case. Address failed checks and verify again.")
            return args


SCHEMAS = {
    "update_plan": {"objective": "string", "steps": "strings", "missing_information": "strings"},
    "search_knowledge": {"query": "string"},
    "lookup_account": {},
    "check_service": {"service": "string"},
    "complete_case": {"summary": "string", "citations": "strings", "action": "string", "reason": "string"},
}
SCHEMAS["verify_case"] = dict(SCHEMAS["complete_case"])
DESCRIPTIONS = {
    "update_plan": "Record a short operational checklist and missing customer information. Call first; revise when observations change the investigation. Not private reasoning.",
    "search_knowledge": "Search the support knowledge base for policies and playbooks.",
    "lookup_account": "Read the current run's scoped customer account and invoice ledger.",
    "check_service": "Read current demo service status. service must be api or exports.",
    "complete_case": "Finish using the exact proposal that passed verify_case after the latest evidence or plan change. Does not execute the action. action must be billing_review, engineering_escalation, identity_review, or support_review.",
    "verify_case": "Check both draft reply and reason against cited evidence: invoice amounts/counts, account plan/lock, service state/region and action policies. If passed is false, collect missing evidence or revise, then verify again. Cite retrieved evidence IDs. This is a mechanical checker, not proof of truth.",
}


def definitions():
    return [{"type": "function", "name": name, "description": DESCRIPTIONS[name], "strict": True,
             "parameters": {"type": "object", "properties": {key: ({"type": "array", "items": {"type": "string"}} if kind == "strings" else {"type": "string"}) for key, kind in schema.items()},
                            "required": list(schema), "additionalProperties": False}}
            for name, schema in SCHEMAS.items()]
