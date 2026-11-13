"""Compare actual retrieval strategies; never score a fallback as semantic."""
import argparse
import getpass
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from supportops.embeddings import MODEL, DIMENSIONS
from supportops.retrieval import Retriever, MODES, RRF_K, corpus_hash
from supportops.retrieval_eval import DATASET, evaluate_retrieval, load_retrieval_cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=["bm25"])
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--prompt-key", action="store_true", help="Hidden local Gemini key entry; key remains in this process")
    parser.add_argument("--output", help="New .json report path; reports are never overwritten")
    args = parser.parse_args()
    if not 1 <= args.k <= 10 or len(args.modes) != len(set(args.modes)):
        parser.error("Use k between 1 and 10 and unique strategies")
    output = Path(args.output) if args.output else ROOT / "reports" / ("retrieval-" + args.split + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".json")
    if output.suffix != ".json" or output.exists():
        parser.error("Choose a new .json report path; existing reports are never overwritten")
    if args.prompt_key:
        if set(args.modes) == {"bm25"} or not sys.stdin.isatty():
            parser.error("Use --prompt-key in an interactive terminal with semantic or hybrid selected")
        if not os.environ.get("GEMINI_API_KEY"):
            key = getpass.getpass("Gemini API key (hidden; process only): ").strip()
            if not key:
                parser.error("No key entered")
            os.environ["GEMINI_API_KEY"] = key
    dataset, cases = load_retrieval_cases(args.split)
    docs = json.loads((ROOT / "data/knowledge.json").read_text())
    strategies = {}
    for mode in args.modes:
        strategies[mode] = evaluate_retrieval(cases, Retriever(docs, mode), args.k)
    report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "dataset_version": dataset["version"], "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest(),
              "dataset_provenance": dataset["provenance"], "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "split": args.split, "k": args.k,
              "corpus_sha256": corpus_hash(docs), "corpus_documents": len(docs),
              "embedding_model": MODEL if set(args.modes) != {"bm25"} else None,
              "dimensions": DIMENSIONS if set(args.modes) != {"bm25"} else None, "rrf_k": RRF_K,
              "engine_sha256": {name: hashlib.sha256((ROOT / "supportops" / name).read_bytes()).hexdigest() for name in ("retrieval.py", "embeddings.py", "retrieval_eval.py")},
              "limitations": ["Synthetic English labels on six fictional documents; not independently human-validated.",
                              "Retrieval metrics do not establish answer correctness or production quality.",
                              "Errors receive zero metric credit; semantic/hybrid runs never use fallback results.",
                              "Latency includes cold document embedding on the first semantic strategy; later strategies may reuse document vectors. Queries are never cached.",
                              "Provider quota and billing apply; token usage and monetary cost are not measured."],
              "strategies": strategies}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"report": str(output), "split": args.split, "k": args.k,
                      "strategies": {mode: {key: value for key, value in result.items() if key != "results"} for mode, result in strategies.items()}}, indent=2))
    return int(any(result["errors"] for result in strategies.values()))


if __name__ == "__main__":
    raise SystemExit(main())
