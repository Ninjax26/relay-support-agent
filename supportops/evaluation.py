"""Reproducible fixture-based decision evaluation; semantic quality needs review."""
import copy
import csv
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from .agent import investigate, PROMPT
from .tools import ROOT, SCHEMAS

DATASET = ROOT / "data/evaluation_cases.json"
CHECKS = ("action", "tool_coverage", "tool_validity", "citation_coverage", "citation_validity", "verification_boundary",
          "response_text", "clarification_proxy", "abstention_proxy")
CLARIFICATION = r"\b(?:please|provide|share|could you|can you tell|what|which|need more|need the)\b"
ABSTENTION = r"cannot|can't|could not|unable|does not establish|not enough|insufficient|unclear|unconfirmed|need more|please (?:share|provide)"


def load_cases(split="dev"):
    payload = json.loads(DATASET.read_text())
    cases = payload["cases"]
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Evaluation IDs must be unique")
    normalized = [re.sub(r"\s+", " ", case["issue"].strip().lower()) for case in cases]
    if len(set(normalized)) != len(normalized):
        raise ValueError("Evaluation issues must not duplicate across splits")
    if split not in {"dev", "test"}:
        raise ValueError("split must be dev or test")
    for case in cases:
        required = {"id", "split", "category", "account_id", "issue", "expected_actions", "required_tools", "required_citations", "expected_text_patterns", "forbidden_text_patterns", "clarification_required", "abstention_required"}
        if not required.issubset(case):
            raise ValueError("Evaluation case is missing required fields")
        if case["split"] not in {"dev", "test"}:
            raise ValueError("Unknown dataset split")
        if not case["expected_actions"] or not set(case["expected_actions"]).issubset({"billing_review", "engineering_escalation", "identity_review", "support_review"}):
            raise ValueError("Unknown expected action")
        if not set(case["required_tools"]).issubset(set(SCHEMAS) | {"check_service:api", "check_service:exports"}):
            raise ValueError("Unknown required tool")
        if not isinstance(case["clarification_required"], bool) or not isinstance(case["abstention_required"], bool):
            raise ValueError("Clarification and abstention labels must be booleans")
        for pattern in case["expected_text_patterns"] + case["forbidden_text_patterns"]:
            re.compile(pattern)
    return payload, [case for case in cases if case["split"] == split]


def grade(case, run):
    if not run or not run.get("answer"):
        return {"checks": {name: False for name in CHECKS}, "passed": False, "failures": ["execution"], "invalid_tool_attempts": sum(not step.get("ok") for step in (run or {}).get("trace", []))}
    answer, trace = run["answer"], run.get("trace", [])
    successful = {step["tool"] for step in trace if step.get("ok")}
    for step in trace:
        if step.get("ok") and step["tool"] == "check_service":
            successful.add("check_service:" + step["arguments"].get("service", ""))
    citations = set(answer.get("citations", []))
    evidence = {source["id"] for source in run.get("evidence", [])}
    text = answer.get("summary", "")
    checks = {
        "action": answer.get("action") in case["expected_actions"],
        "tool_coverage": set(case["required_tools"]).issubset(successful),
        "tool_validity": bool(trace) and all(step.get("ok") and step["tool"] in SCHEMAS for step in trace),
        "citation_coverage": set(case["required_citations"]).issubset(citations),
        "citation_validity": bool(citations) and citations.issubset(evidence),
        "verification_boundary": run.get("status") == "awaiting_approval" and not run.get("ticket_id") and (run.get("verification") or {}).get("passed") is True and "verify_case" in successful and "complete_case" in successful,
        "response_text": all(re.search(pattern, text, re.I) for pattern in case["expected_text_patterns"]) and not any(re.search(pattern, text, re.I) for pattern in case["forbidden_text_patterns"]),
        "clarification_proxy": not case["clarification_required"] or bool(re.search(CLARIFICATION, text, re.I)),
        "abstention_proxy": not case["abstention_required"] or bool(re.search(ABSTENTION, text, re.I)),
    }
    return {"checks": checks, "passed": all(checks.values()), "failures": [name for name, passed in checks.items() if not passed],
            "invalid_tool_attempts": sum(not step.get("ok") for step in trace)}


def evaluate(cases, mode="demo", agent=investigate, repeats=1, max_consecutive_errors=3):
    rows, consecutive_errors, stopped = [], 0, None
    for case in cases:
        for repeat in range(repeats):
            snapshot = {}
            tick = time.monotonic()
            error, run = None, None
            def checkpoint(value):
                snapshot.clear()
                snapshot.update(copy.deepcopy(value))
            try:
                run = agent(case["issue"], case["account_id"], mode, on_step=checkpoint)
                consecutive_errors = 0
            except (ValueError, RuntimeError) as exc:
                error = str(exc)
                consecutive_errors += 1
            except Exception as exc:
                error = f"Unexpected execution failure ({type(exc).__name__}); provider payloads are not exposed."
                consecutive_errors += 1
            duration = round((time.monotonic() - tick) * 1000, 2)
            source = run or snapshot
            rows.append({"case_id": case["id"], "repeat": repeat + 1, "category": case["category"], "split": case["split"],
                         "completed": run is not None, "error": error, "wall_ms": duration, "model": source.get("model"),
                         "usage": source.get("usage", {"input_tokens": 0, "output_tokens": 0}), "usage_partial": error is not None,
                         "grade": grade(case, run or snapshot), "run": run or snapshot})
            if error and ("rate limit or quota" in error.lower() or consecutive_errors >= max_consecutive_errors):
                stopped = "Provider quota/rate limit or consecutive execution errors; remaining cases were not attempted."
                break
        if stopped:
            break
    total = len(rows)
    rates = {name: round(sum(row["grade"]["checks"][name] for row in rows) / total, 4) if total else 0 for name in CHECKS}
    eligible_clarification = [row for row in rows if next(case for case in cases if case["id"] == row["case_id"])["clarification_required"]]
    eligible_abstention = [row for row in rows if next(case for case in cases if case["id"] == row["case_id"])["abstention_required"]]
    for name, eligible in (("clarification_proxy", eligible_clarification), ("abstention_proxy", eligible_abstention)):
        rates[name] = round(sum(row["grade"]["checks"][name] for row in eligible) / len(eligible), 4) if eligible else None
    durations = sorted(row["wall_ms"] for row in rows)
    by_category = {}
    for category in sorted({case["category"] for case in cases}):
        group = [row for row in rows if row["category"] == category]
        by_category[category] = {"attempted": len(group), "completed": sum(row["completed"] for row in group),
                                 "passed": sum(row["grade"]["passed"] for row in group),
                                 "pass_rate": round(sum(row["grade"]["passed"] for row in group) / len(group), 4) if group else None}
    return {"attempted": total, "planned": len(cases) * repeats, "completed": sum(row["completed"] for row in rows),
            "passed": sum(row["grade"]["passed"] for row in rows), "pass_rate": round(sum(row["grade"]["passed"] for row in rows) / total, 4) if total else 0,
            "check_rates": rates, "eligible_clarification": len(eligible_clarification), "eligible_abstention": len(eligible_abstention),
            "invalid_tool_attempts": sum(row["grade"]["invalid_tool_attempts"] for row in rows),
            "mean_wall_ms": round(sum(durations) / total, 2) if total else 0,
            "p95_wall_ms": durations[min(total - 1, int(total * 0.95))] if total else 0,
            "tokens": {key: sum(row["usage"].get(key, 0) for row in rows) for key in ("input_tokens", "output_tokens")},
            "by_category": by_category, "stopped_early": stopped, "results": rows}


def report_metadata(dataset, split, mode, selected, provider=None):
    return {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "dataset_version": dataset["version"],
            "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest(), "dataset_provenance": dataset["provenance"],
            "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
            "engine_sha256": {name: hashlib.sha256((ROOT / "supportops" / name).read_bytes()).hexdigest() for name in ("agent.py", "tools.py", "evaluation.py")},
            "fixture_sha256": {name: hashlib.sha256((ROOT / "data" / name).read_bytes()).hexdigest() for name in ("knowledge.json", "accounts.json", "services.json")},
            "split": split, "mode": mode, "provider": provider, "selected_cases": selected,
            "configured_model": os.environ.get("GEMINI_MODEL", "gemini-3.8-flash") if provider == "gemini" else os.environ.get("OPENAI_MODEL", "gpt-4.1-mini") if provider == "openai" else "deterministic-demo",
            "limitations": ["Synthetic cases labeled against fixtures; not independently human-validated.", "Automatic checks measure routing, evidence coverage and English text proxies, not semantic answer correctness.", "A limited run does not represent the entire test split.", "Usage for failed requests may be missing; recorded token totals are not monetary cost."]}


def write_report(report, output, cases):
    path = Path(output)
    if path.suffix != ".json":
        raise ValueError("Report output must end in .json")
    review_path = path.with_suffix(".review.csv")
    if path.exists() or review_path.exists():
        raise FileExistsError("Choose a new report path; existing results are never overwritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Raw answers and traces remain local; never include provider credentials/history.
    with path.open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    indexed = {case["id"]: case for case in cases}
    with review_path.open("x", newline="") as handle:
        fields = ["case_id", "repeat", "category", "issue", "answer", "expected_actions", "required_citations", "automatic_failures", "evidence", "groundedness_correct", "resolution_useful", "abstention_appropriate", "review_comments"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in report["results"]:
            case, run = indexed[result["case_id"]], result["run"]
            writer.writerow({"case_id": case["id"], "repeat": result["repeat"], "category": case["category"], "issue": case["issue"],
                             "answer": (run.get("answer") or {}).get("summary", ""), "expected_actions": ";".join(case["expected_actions"]),
                             "required_citations": ";".join(case["required_citations"]), "automatic_failures": ";".join(result["grade"]["failures"]),
                             "evidence": json.dumps(run.get("evidence", [])), "groundedness_correct": "", "resolution_useful": "", "abstention_appropriate": "", "review_comments": ""})
    return path, review_path


def summarize_review(report, review_path):
    """Aggregate human yes/no judgments separately from automatic scores."""
    metrics = {name: {"yes": 0, "no": 0, "na": 0, "blank": 0} for name in ("groundedness_correct", "resolution_useful", "abstention_appropriate")}
    expected = {(row["case_id"], row["repeat"]): row for row in report["results"]}
    seen = set()
    with Path(review_path).open(newline="") as handle:
        for row in csv.DictReader(handle):
            key = (row["case_id"], int(row["repeat"]))
            if key not in expected or key in seen:
                raise ValueError("Review contains an unknown or duplicate case/repeat")
            seen.add(key)
            original = expected[key]
            if row["answer"] != (original["run"].get("answer") or {}).get("summary", ""):
                raise ValueError("Review answer does not match this report")
            for metric, counts in metrics.items():
                value = row[metric].strip().lower()
                if value not in {"", "yes", "no", "na"}:
                    raise ValueError("Human judgments must be yes, no, na, or blank")
                if not original["completed"] and value in {"yes", "no"}:
                    raise ValueError("Do not grade semantic quality for an incomplete answer")
                counts[value or "blank"] += 1
    completed = report["completed"]
    for counts in metrics.values():
        graded = counts["yes"] + counts["no"]
        counts["reviewed"] = graded
        counts["rate"] = round(counts["yes"] / graded, 4) if graded else None
        counts["coverage_of_completed"] = round(graded / completed, 4) if completed else None
    return {"dataset_version": report["dataset_version"], "dataset_sha256": report["dataset_sha256"], "mode": report["mode"],
            "split": report["split"], "completed_answers": completed, "review_rows": len(seen), "human_review": metrics,
            "limitation": "Human judgments are separate from automatic checks; unreviewed and not-applicable answers do not count as correct."}
