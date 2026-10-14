"""Summarize human quality judgments for an existing evaluation report."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from supportops.evaluation import summarize_review


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="Original quality report JSON")
    parser.add_argument("review", help="Annotated human-review CSV")
    args = parser.parse_args()
    try:
        report = json.loads(Path(args.report).read_text())
        print(json.dumps(summarize_review(report, args.review), indent=2))
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
