"""Read-only fixtures, lexical retrieval, and strict tool argument validation."""
import json
import math
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def tokens(text):
    return re.findall(r"[a-z0-9]+", text.lower())


class ToolError(ValueError):
    pass


class Tools:
    def __init__(self, account_id):
        self.account_id = account_id
        self.docs = json.loads((ROOT / "data/knowledge.json").read_text())
        self.accounts = json.loads((ROOT / "data/accounts.json").read_text())
        self.services = json.loads((ROOT / "data/services.json").read_text())
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
        summary = proposal["summary"]
        if re.search(r"\b(?:I|we) (?:have )?(?:issued|processed|completed) (?:a |the |your )?refund|(?<!no )\brefund (?:has been|was) issued|\b(?:I|we) (?:have )?(?:unlocked|cancelled|canceled) (?:your|the) (?:account|subscription)", summary, re.I):
            issues.append("Do not claim customer-impacting actions have been executed.")
        facts = json.dumps(list(self.evidence.values()))
        for identifier in set(re.findall(r"\b(?:INV|INC)-[A-Za-z0-9]+\b", summary)):
            if identifier not in facts:
                issues.append(f"Identifier {identifier} is not present in retrieved evidence.")
        if action == "support_review" and "export" in summary.lower():
            for ref in {"ACCOUNT", "KB-104", "STATUS-exports"} - set(refs):
                issues.append(f"Retrieve and cite {ref} for an export investigation.")
        self.verification = {"passed": not issues, "issues": issues, "checks": ["plan present", "retrieved citations", "action evidence coverage", "known incident/invoice IDs", "limited action-claim policy"],
                             "limitation": "These are mechanical checks, not proof of semantic groundedness."}
        self.verified_proposal = json.dumps(proposal, sort_keys=True) if not issues else None
        return self.verification

    def search(self, query):
        """BM25 ranking: inexpensive, explainable retrieval for a small corpus."""
        corpus = [tokens(d["title"] + " " + d["text"]) for d in self.docs]
        average = sum(map(len, corpus)) / len(corpus)
        results = []
        for doc, words in zip(self.docs, corpus):
            counts = Counter(words)
            score = 0.0
            for term in set(tokens(query)):
                df = sum(term in item for item in corpus)
                idf = math.log(1 + (len(corpus) - df + 0.5) / (df + 0.5))
                tf = counts[term]
                score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * len(words) / average))
            if score > 0:
                results.append({**doc, "score": round(score, 3)})
        results.sort(key=lambda d: (-d["score"], d["id"]))
        selected = results[:3]
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
            return {"recorded": True, "revision": len(self.plan_history), **args}
        if name in {"search_knowledge", "lookup_account", "check_service"}:
            self.verified_proposal = None
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
    "verify_case": "Check a draft proposal against required evidence and limited action policies. If passed is false, collect missing evidence or revise, then verify again. Cite retrieved evidence IDs. This is a mechanical checker, not proof of truth.",
}


def definitions():
    return [{"type": "function", "name": name, "description": DESCRIPTIONS[name], "strict": True,
             "parameters": {"type": "object", "properties": {key: ({"type": "array", "items": {"type": "string"}} if kind == "strings" else {"type": "string"}) for key, kind in schema.items()},
                            "required": list(schema), "additionalProperties": False}}
            for name, schema in SCHEMAS.items()]
