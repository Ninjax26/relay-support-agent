"""Labeled retrieval evaluation, independent of agent answer-quality scoring."""
import json
import math
import time
from pathlib import Path
from .embeddings import EmbeddingError

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data/retrieval_cases.json"
METRICS = ("recall_at_k", "mrr_at_k", "ndcg_at_k")


def load_retrieval_cases(split="dev"):
    if split not in {"dev", "test"}:
        raise ValueError("Choose the dev or test retrieval split")
    dataset = json.loads(DATASET.read_text())
    corpus_ids = {doc["id"] for doc in json.loads((ROOT / "data/knowledge.json").read_text())}
    seen = set()
    for case in dataset["cases"]:
        if not isinstance(case.get("id"), str) or case["id"] in seen or case.get("split") not in {"dev", "test"} or not isinstance(case.get("query"), str) or not 1 <= len(case["query"].strip()) <= 4000 or not isinstance(case.get("category"), str):
            raise ValueError("Invalid retrieval case ID, query or split")
        refs = case.get("relevant_ids")
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in corpus_ids for ref in refs) or len(refs) != len(set(refs)):
            raise ValueError("Retrieval labels must identify unique corpus documents")
        seen.add(case["id"])
    selected = [case for case in dataset["cases"] if case["split"] == split]
    if not selected:
        raise ValueError("The retrieval split is empty")
    return dataset, selected


def rank_metrics(ids, relevant_ids, k):
    if not relevant_ids or type(k) is not int or k < 1:
        raise ValueError("Metrics require relevance labels and positive k")
    relevant = set(relevant_ids)
    hits, seen = [], set()
    for rank, identifier in enumerate(ids[:k], 1):
        if identifier in relevant and identifier not in seen:
            hits.append(rank)
        seen.add(identifier)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(k, len(relevant)) + 1))
    return {"recall_at_k": len(hits) / len(relevant), "mrr_at_k": 1 / hits[0] if hits else 0,
            "ndcg_at_k": sum(1 / math.log2(rank + 1) for rank in hits) / ideal}


def evaluate_retrieval(cases, retriever, k=3):
    rows = []
    for case in cases:
        started = time.monotonic()
        try:
            hits, metadata = retriever.search(case["query"], k=k, allow_fallback=False)
            identifiers = [row["id"] for row in hits]
            error = None
        except EmbeddingError as exc:
            identifiers, metadata, error = [], None, exc.code
        rows.append({"case_id": case["id"], "category": case["category"], "ids": identifiers,
                     "relevant_ids": case["relevant_ids"], "error": error, "retrieval": metadata,
                     "wall_ms": round((time.monotonic() - started) * 1000, 3),
                     **rank_metrics(identifiers, case["relevant_ids"], k)})
    total = len(rows)
    durations = sorted(row["wall_ms"] for row in rows)
    return {"mode": retriever.mode, "planned": len(cases), "attempted": total,
            "completed": sum(row["error"] is None for row in rows), "errors": sum(row["error"] is not None for row in rows),
            **{name: sum(row[name] for row in rows) / total if total else None for name in METRICS},
            "mean_wall_ms": sum(durations) / total if total else None,
            "p95_wall_ms": durations[min(total - 1, math.ceil(total * .95) - 1)] if total else None,
            "results": rows}
