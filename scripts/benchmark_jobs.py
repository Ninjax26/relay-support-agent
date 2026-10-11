"""Deterministic job-queue load benchmark using temporary storage."""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from supportops.jobs import JobRunner
from supportops.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=50)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 200 or not 1 <= args.workers <= 16:
        parser.error("jobs must be 1–200 and workers 1–16")
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "load.sqlite3")
        runner = JobRunner(store, workers=args.workers, capacity=args.jobs)
        latencies, ids = [], []
        started = time.monotonic()
        for index in range(args.jobs):
            tick = time.monotonic()
            run = runner.submit({"message": "API timeout", "account_id": "acct_demo", "mode": "demo"}, f"load-{index}")
            latencies.append((time.monotonic() - tick) * 1000)
            ids.append(run["id"])
        runner.start()
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                counts = store.metrics()["jobs"]
                if counts.get("awaiting_approval", 0) + counts.get("failed", 0) + counts.get("interrupted", 0) == args.jobs:
                    break
                time.sleep(0.05)
            metrics = store.metrics()
            states = [store.get(run_id) for run_id in ids]
            completed = sum(run["status"] == "awaiting_approval" for run in states)
            report = {"mode": "deterministic-demo", "scope": "local SQLite worker queue; excludes LLM and HTTP latency", "jobs": args.jobs,
                      "workers": args.workers, "completed": completed, "wall_seconds": round(time.monotonic() - started, 3),
                      "enqueue_p95_ms": round(sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))], 3),
                      "tickets_without_approval": metrics["tickets"], "checkpoints_saved": all(len(run["trace"]) == 7 for run in states)}
            print(json.dumps(report, indent=2))
            return 0 if completed == args.jobs and metrics["tickets"] == 0 and report["checkpoints_saved"] else 1
        finally:
            runner.close()


if __name__ == "__main__":
    raise SystemExit(main())
