"""Bounded English consistency rules over cited structured evidence.

These rules detect specific contradictions, not arbitrary textual entailment.
Unrecognized assertions remain subject to human review and live evaluation.
"""
import re
import unicodedata
from collections import Counter
from decimal import Decimal

CHECKS = ["cited structured identifiers", "invoice amounts, currencies and settled counts",
          "account plan and lock state", "export eligibility", "service state and incident region",
          "unexecuted action and refund policy", "unsupported recovery estimates"]
LIMITATION = ("Rule-based consistency checks cover recognized English claims in the reply and review reason. "
              "Unrecognized paraphrases, causality, policy interpretation and general semantic groundedness "
              "are not verified. A passing result still requires human review.")


def check_proposal(proposal, evidence):
    """Return actionable failures using ONLY records cited by this proposal."""
    findings = []
    account = evidence.get("ACCOUNT", {})
    payments = account.get("payments", [])
    invoices = {p["invoice"].upper() for p in payments}
    incidents = {v["incident_id"].upper() for key, v in evidence.items()
                 if key.startswith("STATUS-") and v.get("incident_id")}

    def fail(code, field, claim, source, message):
        findings.append({"code": code, "field": field, "claim": claim,
                         "evidence": source, "message": f"{field}: {message}"})

    for field in ("summary", "reason"):
        text = unicodedata.normalize("NFKC", proposal[field]).replace("’", "'")
        for identifier in set(re.findall(r"\b(?:INV|INC)-[a-z0-9]+\b", text, re.I)):
            if identifier.upper() not in invoices | incidents:
                fail("identifier", field, identifier, [], f"Identifier {identifier} is not present in cited structured evidence. Retrieve and cite the matching account/service record or remove it.")

        # Split clauses so one negation cannot suppress an unrelated assertion.
        clauses = re.split(r"(?<!\d)[.!?;]|\n|,\s*(?:but|however)\s+", text, flags=re.I)
        for clause in clauses:
            clause = clause.strip()
            if not clause:
                continue
            lower = clause.lower()
            # No mutation tools exist: positive completion claims are unsupported.
            completed = [
                r"\b(?:i|we) (?:have |already )?(?:issued|processed|completed|approved) (?:a |the |your )?refund\b",
                r"\b(?:i|we) (?:have |already )?(?:unlocked|cancelled|canceled|rerun|restarted)\b",
                r"\b(?:(?:your|the|a) )?refund (?:has been |was |is (?:now )?)(?:issued|processed|completed|approved)\b",
                r"\b(?:your |the )?(?:account|subscription|(?:export )?job) (?:has been |was |is (?:now )?)(?:unlocked|cancelled|canceled|rerun|restarted)\b",
                r"\b(?:you (?:will|shall) (?:receive|get)|(?:a |your |the )?refund (?:is |has been )?(?:guaranteed|approved)|we (?:will|shall) (?:issue|process)) (?:a |the |your )?refund\b",
                r"\b(?:a |your |the )?refund (?:is |has been )?(?:guaranteed|approved)\b",
                r"\b(?:i|we) (?:have |already )?(?:initiated|started|opened|created|submitted) (?:a |an |the )?(?:(?:billing|support|identity|engineering|manual) )?(?:review|ticket|escalation)\b",
                r"\b(?:(?:a|the) )?(?:(?:billing|support|identity|engineering|manual) )?(?:review|ticket|escalation) (?:has been|was|is now) (?:initiated|started|opened|created|submitted)\b",
                r"\b(?:will|shall) (?:review (?:the|this|your) [^.!?]{1,60} and )?(?:contact you|reach out to you|follow up with you)\b",
            ]
            for pattern in completed:
                for match in re.finditer(pattern, clause, re.I):
                    prefix = clause[max(0, match.start() - 12):match.start()]
                    if re.search(r"\b(?:no|not|never)\s+$", prefix, re.I):
                        continue
                    fail("action_policy", field, match[0], [], "Describe review as a proposal. Remove claims that actions were executed or that refunds or customer follow-up are guaranteed.")
                    break
                else:
                    continue
                break
            if re.search(r"\b(?:will (?:recover|be (?:restored|resolved|fixed|operational))|(?<!no )(?:recovery|resolution|fix|restoration) (?:time|estimate|eta) (?:is|of)(?! (?:not|unavailable|unknown|available))|(?:recover|restored|resolved|fixed) (?:in|by|within))\b", lower):
                fail("recovery_estimate", field, clause, [], "No structured recovery estimate is available. Remove the recovery deadline or promise.")

            # Compare money with the same invoice, never an unrelated payment.
            mentioned = set(re.findall(r"\bINV-[a-z0-9]+\b", clause, re.I))
            scoped = [p for p in payments if not mentioned or p["invoice"].upper() in {v.upper() for v in mentioned}]
            for match in re.finditer(r"\b(USD|EUR|GBP|INR|CAD|AUD|JPY|CHF|CNY)\s*(\d+(?:,\d{3})*(?:\.\d+)?)\b|([$€£])\s*(\d+(?:,\d{3})*(?:\.\d+)?)\b|\b(\d+(?:\.\d+)?)\s*(USD|EUR|GBP)\b", clause):
                currency = match[1] or {"$": "USD", "€": "EUR", "£": "GBP"}.get(match[3]) or match[6]
                amount = Decimal((match[2] or match[4] or match[5]).replace(",", ""))
                valid = {(p["currency"], Decimal(str(p["amount"]))) for p in scoped}
                if re.search(r"\btotal\b", lower):
                    currencies = {p["currency"] for p in scoped}
                    valid = {(c, sum((Decimal(str(p["amount"])) for p in scoped if p["currency"] == c and p["state"] == "settled"), Decimal(0))) for c in currencies}
                if (currency, amount) not in valid:
                    fail("payment", field, match[0], ["ACCOUNT"], "Payment amount/currency is unsupported by the cited account ledger for this invoice. Use the observed values or omit the amount.")
            count = re.search(r"\b(one|two|three|four|five|\d+)\s+settled\s+(?:(USD|EUR|GBP)\s+(\d+(?:\.\d+)?)\s+)?payments?\b", clause, re.I)
            settled = [p for p in scoped if p["state"] == "settled"]
            if count:
                number = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}.get(count[1].lower())
                number = int(count[1]) if number is None else number
                counted = [p for p in settled if not count[2] or (p["currency"] == count[2].upper() and Decimal(str(p["amount"])) == Decimal(count[3]))]
                if not account or number != len(counted):
                    fail("payment", field, count[0], ["ACCOUNT"], "The settled payment count does not match the cited account ledger.")
            duplicate = re.search(r"\b(?:shows?|confirms?|confirmed|found|has|are)\s+(?:a\s+)?duplicate (?:settled )?(?:payments?|charges?)\b", clause, re.I)
            counts = Counter(p["invoice"] for p in settled)
            if duplicate and not re.search(r"\b(?:not|never)\s+$", clause[max(0, duplicate.start() - 12):duplicate.start()], re.I):
                if not any(n > 1 for n in counts.values()):
                    fail("payment", field, clause, ["ACCOUNT"], "The cited ledger does not establish duplicate settled payments for one invoice.")

            if re.search(r"\bno duplicate (?:settled )?(?:payments?|charges?)\b", clause, re.I):
                if not account or any(n > 1 for n in counts.values()):
                    fail("payment", field, clause, ["ACCOUNT"], "An explicit denial of duplicate payments contradicts or lacks the cited ledger. Express uncertainty if evidence is incomplete.")

            for match in re.finditer(r"\b(?:your|the|current)\s+(?:account\s+)?plan\s+is\s+(Basic|Pro|Business)\b", clause, re.I):
                if match[1].lower() != str(account.get("plan", "")).lower():
                    fail("account_plan", field, match[0], ["ACCOUNT"], "The claimed plan does not match the cited account. Retrieve and cite ACCOUNT and use its plan.")
            for match in re.finditer(r"\b(?:your|the) account is (not (?:marked )?|marked )?locked\b", clause, re.I):
                locked = not (match[1] or "").lower().startswith("not")
                if "locked" not in account or account["locked"] != locked:
                    fail("account_locked", field, match[0], ["ACCOUNT"], "The claimed lock state does not match the cited account.")
            eligibility = re.search(r"\b(?:your|the current|current) (?:account |plan )?(?:plan )?(supports?|is (?:not )?eligible for) exports\b", clause, re.I)
            if eligibility:
                eligible = "not" not in eligibility[0].lower()
                supported = account.get("plan") in {"Pro", "Business"}
                if not account or "KB-104" not in evidence or eligible != supported:
                    fail("export_eligibility", field, eligibility[0], ["ACCOUNT", "KB-104"], "Export eligibility needs the cited account plan and KB-104; only Pro and Business are eligible.")

            for match in re.finditer(r"\b(api|exports?)\s+(?:service\s+)?is\s+(not\s+)?(operational|healthy|degraded|down)\b", clause, re.I):
                ref = "STATUS-api" if match[1].lower() == "api" else "STATUS-exports"
                state = "operational" if match[3].lower() == "healthy" else match[3].lower()
                observed = evidence.get(ref, {}).get("state")
                if observed is None or (observed == state) == bool(match[2]):
                    fail("service_state", field, match[0], [ref], f"Service state contradicts or lacks {ref}. Retrieve and cite the matching service status.")
            regions = re.findall(r"\b[a-z]{2}(?:-[a-z]+)+-\d+\b", clause, re.I)
            for region in regions:
                service_refs = {"STATUS-api"} if re.search(r"\bapi\b", lower) else {"STATUS-exports"} if re.search(r"\bexports?\b", lower) else {key for key in evidence if key.startswith("STATUS-")}
                incident_ids = set(re.findall(r"\bINC-[a-z0-9]+\b", clause, re.I))
                records = [evidence[ref] for ref in service_refs if ref in evidence and (not incident_ids or str(evidence[ref].get("incident_id", "")).upper() in {v.upper() for v in incident_ids})]
                if not any(str(record.get("region", "")).lower() == region.lower() for record in records):
                    fail("incident_region", field, region, sorted(service_refs), "The claimed region is not supported by the cited service/incident record.")
    return findings
