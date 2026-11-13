import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from supportops.retrieval_eval import rank_metrics, evaluate_retrieval, load_retrieval_cases
from supportops.retrieval import Retriever
from test_retrieval import DOCS, VectorFixture
from supportops.retrieval import DocumentCache

ROOT = Path(__file__).resolve().parents[1]


class RetrievalEvaluationTests(unittest.TestCase):
    def test_metrics_use_relevance_denominator_and_rank_discount(self):
        metrics = rank_metrics(["X", "A", "B"], ["A", "B"], 3)
        self.assertEqual(metrics["recall_at_k"], 1)
        self.assertEqual(metrics["mrr_at_k"], 0.5)
        self.assertAlmostEqual(metrics["ndcg_at_k"], 0.6934264036)
        self.assertAlmostEqual(rank_metrics(["X", "X", "A"], ["A"], 3)["mrr_at_k"], 1 / 3)
        self.assertEqual(rank_metrics([], ["A"], 3)["recall_at_k"], 0)
        self.assertEqual(rank_metrics(["A", "A"], ["A", "B"], 3)["recall_at_k"], 0.5)

    def test_strict_benchmark_does_not_score_fallback_as_semantic(self):
        from unittest.mock import patch
        cases = [{"id": "one", "query": "payment", "relevant_ids": ["A"], "category": "billing"}]
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            result = evaluate_retrieval(cases, Retriever(DOCS, "semantic"), 3)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["recall_at_k"], 0)
        self.assertEqual(result["results"][0]["error"], "missing_key")

    def test_vector_fixture_scores_retrieval_not_agent_answers(self):
        cases = [{"id": "one", "query": "workspace access", "relevant_ids": ["B"], "category": "access"}]
        result = evaluate_retrieval(cases, Retriever(DOCS, "semantic", VectorFixture(), DocumentCache()), 1)
        self.assertEqual(result["mrr_at_k"], 1)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["results"][0]["ids"], ["B"])

    def test_dataset_split_is_explicit_and_labels_belong_to_corpus(self):
        dataset, cases = load_retrieval_cases("dev")
        self.assertEqual(len(cases), 12)
        self.assertTrue(all(case["split"] == "dev" for case in cases))
        self.assertEqual(len(dataset["cases"]), 24)
        with self.assertRaises(ValueError):
            load_retrieval_cases("invalid")

    def test_cli_bm25_report_is_reproducible_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "retrieval.json"
            command = [sys.executable, str(ROOT / "scripts/benchmark_retrieval.py"), "--modes", "bm25", "--output", str(output)]
            first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report["split"], "dev")
            self.assertEqual(report["strategies"]["bm25"]["planned"], 12)
            self.assertIn("corpus_sha256", report)
            second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(json.loads(output.read_text()), report)


if __name__ == "__main__":
    unittest.main()
