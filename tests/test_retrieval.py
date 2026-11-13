"""Retrieval math/control-flow tests; vector fixtures are not ML-quality evidence."""
import io
import json
import math
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from supportops.embeddings import GeminiEmbedder, EmbeddingError, normalize_vectors
from supportops.retrieval import Retriever, DocumentCache, reciprocal_rank_fusion
from supportops.tools import Tools

DOCS = [
    {"id": "A", "title": "Invoice", "text": "duplicate payment ledger"},
    {"id": "B", "title": "Access", "text": "password reset login"},
    {"id": "C", "title": "Status", "text": "api timeout outage"},
]


class VectorFixture:
    model = "fixture-not-a-real-model"
    dimensions = 2

    def __init__(self, namespace="fixture"):
        self.cache_key = (namespace, self.model, self.dimensions)
        self.document_calls = 0

    def embed(self, texts, task):
        if task == "RETRIEVAL_DOCUMENT":
            self.document_calls += 1
            return [[1, 0], [0, 1], [-1, 0]][:len(texts)]
        return [[0, 2] for _ in texts]


class RetrievalTests(unittest.TestCase):
    def test_bm25_ranking_and_no_match_stay_offline(self):
        engine = Retriever(DOCS)
        hits, meta = engine.search("payment ledger")
        self.assertEqual([row["id"] for row in hits], ["A"])
        self.assertEqual(meta["effective_mode"], "bm25")
        self.assertEqual(engine.search("zzzzzz")[0], [])

    def test_semantic_uses_cosine_not_vector_magnitude(self):
        engine = Retriever(DOCS, "semantic", VectorFixture(), DocumentCache())
        hits, meta = engine.search("I cannot get into my workspace")
        self.assertEqual([row["id"] for row in hits], ["B", "A", "C"])
        self.assertAlmostEqual(hits[0]["score"], 1)
        self.assertEqual(meta["effective_mode"], "semantic")
        self.assertFalse(meta["fallback"])

    def test_fusion_rewards_agreement_and_breaks_ties_by_id(self):
        ranked = reciprocal_rank_fusion([["A", "B"], ["C", "B"]])
        self.assertEqual([row[0] for row in ranked], ["B", "A", "C"])
        self.assertAlmostEqual(ranked[0][1], 2 / 62)
        self.assertEqual(reciprocal_rank_fusion([[], []]), [])

    def test_hybrid_records_both_rankings(self):
        hits, meta = Retriever(DOCS, "hybrid", VectorFixture(), DocumentCache()).search("password")
        self.assertEqual(hits[0]["id"], "B")
        self.assertEqual(hits[0]["retrieval"]["bm25_rank"], 1)
        self.assertEqual(hits[0]["retrieval"]["semantic_rank"], 1)
        self.assertEqual(meta["effective_mode"], "hybrid")

    def test_missing_key_falls_back_explicitly_but_strict_search_fails(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            engine = Retriever(DOCS, "hybrid")
            hits, meta = engine.search("payment")
            self.assertEqual(hits[0]["id"], "A")
            self.assertEqual(meta["effective_mode"], "bm25")
            self.assertEqual(meta["fallback"], "missing_key")
            with self.assertRaises(EmbeddingError):
                engine.search("payment", allow_fallback=False)

    def test_corpus_and_provider_identity_invalidate_document_cache(self):
        cache, provider = DocumentCache(), VectorFixture()
        first = Retriever(DOCS, "semantic", provider, cache)
        self.assertEqual(first.search("help")[0][0]["id"], "B")
        first.search("another query")
        self.assertEqual(provider.document_calls, 1)
        changed = [{**row, "text": row["text"] + " revised"} for row in DOCS]
        Retriever(changed, "semantic", provider, cache).search("help")
        self.assertEqual(provider.document_calls, 2)
        other = VectorFixture("different-provider-scope")
        Retriever(DOCS, "semantic", other, cache).search("help")
        self.assertEqual(other.document_calls, 1)

    def test_document_cache_evicts_old_indexes(self):
        cache = DocumentCache(capacity=1)
        first, second = VectorFixture("first"), VectorFixture("second")
        Retriever(DOCS, "semantic", first, cache).search("help")
        Retriever(DOCS, "semantic", second, cache).search("help")
        self.assertEqual(Retriever(DOCS, "semantic", first, cache).search("help")[0][0]["id"], "B")
        self.assertEqual(first.document_calls, 2)

    def test_model_change_invalidates_document_cache(self):
        cache, provider = DocumentCache(), VectorFixture()
        Retriever(DOCS, "semantic", provider, cache).search("help")
        provider.model = "another-fixture-model"
        Retriever(DOCS, "semantic", provider, cache).search("help")
        self.assertEqual(provider.document_calls, 2)

    def test_concurrent_workers_share_one_document_index(self):
        cache, provider = DocumentCache(), VectorFixture()
        def search(_):
            return Retriever(DOCS, "semantic", provider, cache).search("help")[0][0]["id"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(search, range(8))), ["B"] * 8)
        self.assertEqual(provider.document_calls, 1)

    def test_bad_vectors_are_rejected_before_similarity(self):
        for values in ([[0, 0]], [[math.nan, 1]], [[math.inf, 1]], [[True, 1]], [["x", 1]], [[10**1000, 1]], [[1]]):
            with self.subTest(values=values), self.assertRaises(EmbeddingError):
                normalize_vectors(values, 1, 2)
        self.assertEqual(normalize_vectors([[3, 4]], 1, 2), [(0.6, 0.8)])

    def test_invalid_configuration_and_empty_query_fail_locally(self):
        for mode in ("invalid", ""):
            with self.assertRaises(ValueError):
                Retriever(DOCS, mode)
        with self.assertRaises(ValueError):
            Retriever(DOCS).search(" ")
        with self.assertRaises(ValueError):
            Retriever(DOCS).search("payment", k=0)
        with self.assertRaises(ValueError):
            Retriever([DOCS[0], DOCS[0]])

    def test_tool_search_retains_citations_and_retrieval_provenance(self):
        with patch.dict("os.environ", {"RELAY_RETRIEVAL_MODE": "hybrid", "GEMINI_API_KEY": ""}):
            tools = Tools("acct_demo")
            hits = tools.call("search_knowledge", {"query": "duplicate payment"})
            self.assertIn("KB-101", tools.evidence)
            self.assertEqual(hits[0]["retrieval"]["effective_mode"], "bm25")
            self.assertEqual(tools.retrieval_events[-1]["fallback"], "missing_key")

    def test_demo_remains_offline_when_hybrid_is_configured(self):
        from supportops.agent import investigate
        with patch.dict("os.environ", {"RELAY_RETRIEVAL_MODE": "hybrid", "GEMINI_API_KEY": "test-key-not-a-credential"}):
            with patch("urllib.request.urlopen", side_effect=AssertionError("Demo attempted network access")):
                run = investigate("Duplicate charge", "acct_demo")
        self.assertEqual(run["retrieval"][-1]["effective_mode"], "bm25")
        self.assertEqual(run["status"], "awaiting_approval")

    def test_provider_failure_is_visible_in_fallback_results(self):
        provider = VectorFixture()
        def unavailable(texts, task):
            raise EmbeddingError("quota")
        provider.embed = unavailable
        hits, meta = Retriever(DOCS, "hybrid", provider, DocumentCache()).search("payment")
        self.assertEqual(hits[0]["id"], "A")
        self.assertEqual(meta["effective_mode"], "bm25")
        self.assertEqual(meta["fallback"], "quota")

    def test_empty_corpus_and_invalid_vectors_do_not_reach_ranking(self):
        with self.assertRaises(ValueError):
            Retriever([])
        provider = VectorFixture()
        provider.embed = lambda texts, task: [[1]] * len(texts)
        hits, meta = Retriever(DOCS, "semantic", provider, DocumentCache()).search("payment")
        self.assertEqual(meta["fallback"], "invalid_vectors")
        self.assertEqual(hits[0]["id"], "A")


class EmbeddingTransportTests(unittest.TestCase):
    def test_transport_maps_tasks_and_normalizes_real_response(self):
        vector = [3, 4] + [0] * 766
        captured = []
        def respond(request, timeout):
            captured.append(json.loads(request.data))
            self.assertNotIn("?", request.full_url)
            return io.BytesIO(json.dumps({"embeddings": [{"values": vector}]}).encode())
        with patch("urllib.request.urlopen", side_effect=respond):
            provider = GeminiEmbedder("test-key-not-a-credential")
            self.assertAlmostEqual(provider.embed(["invoice"], "RETRIEVAL_DOCUMENT")[0][0], 0.6)
            self.assertAlmostEqual(provider.embed(["charged twice"], "RETRIEVAL_QUERY")[0][1], 0.8)
        self.assertEqual(captured[0]["requests"][0]["taskType"], "RETRIEVAL_DOCUMENT")
        self.assertEqual(captured[1]["requests"][0]["taskType"], "RETRIEVAL_QUERY")
        self.assertEqual(captured[0]["requests"][0]["outputDimensionality"], 768)

    def test_http_and_malformed_responses_are_sanitized(self):
        provider = GeminiEmbedder("test-key-not-a-credential")
        error = urllib.error.HTTPError("https://example.invalid", 429, "private provider detail", {}, io.BytesIO(b"private provider detail"))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(EmbeddingError) as caught:
                provider.embed(["query"], "RETRIEVAL_QUERY")
            self.assertEqual(caught.exception.code, "quota")
            self.assertNotIn("private provider detail", str(caught.exception))
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"embeddings": []}')):
            with self.assertRaises(EmbeddingError):
                provider.embed(["query"], "RETRIEVAL_QUERY")


if __name__ == "__main__":
    unittest.main()
