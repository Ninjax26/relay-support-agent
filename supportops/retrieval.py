"""BM25, optional cosine search and reciprocal rank fusion for small corpora."""
import hashlib
import json
import math
import os
import re
import threading
import time
from collections import Counter, OrderedDict
from .embeddings import GeminiEmbedder, EmbeddingError, normalize_vectors

MODES = ("bm25", "semantic", "hybrid")
RRF_K = 60


def corpus_hash(docs):
    return hashlib.sha256(json.dumps(docs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def configured_mode():
    mode = os.environ.get("RELAY_RETRIEVAL_MODE", "bm25").lower()
    if mode not in MODES:
        raise ValueError("RELAY_RETRIEVAL_MODE must be bm25, semantic or hybrid")
    return mode


def tokens(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def reciprocal_rank_fusion(rankings):
    scores = Counter()
    for ranking in rankings:
        seen = set()
        for rank, identifier in enumerate(ranking, 1):
            if identifier not in seen:
                scores[identifier] += 1 / (RRF_K + rank)
                seen.add(identifier)
    return sorted(scores.items(), key=lambda row: (-row[1], row[0]))


class DocumentCache:
    """Bounded process memory, shared across workers; never caches queries."""
    def __init__(self, capacity=4):
        self.capacity = capacity
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def get_or_create(self, key, load):
        # Serialize cold builds to avoid duplicate document embedding requests.
        with self.lock:
            if key not in self.entries:
                self.entries[key] = load()
                while len(self.entries) > self.capacity:
                    self.entries.popitem(last=False)
            self.entries.move_to_end(key)
            return self.entries[key]


DOCUMENT_CACHE = DocumentCache()


class Retriever:
    def __init__(self, docs, mode="bm25", embedder=None, cache=None):
        if mode not in MODES:
            raise ValueError("Retrieval mode must be bm25, semantic or hybrid")
        if not docs or len(docs) > 100 or any(not all(isinstance(d.get(key), str) and d[key].strip() for key in ("id", "title", "text")) for d in docs) or len({d["id"] for d in docs}) != len(docs):
            raise ValueError("Provide 1–100 unique knowledge documents with id, title and text")
        self.docs = [{key: d[key] for key in ("id", "title", "text")} for d in docs]
        self.mode, self.embedder = mode, embedder
        self.cache = cache if cache is not None else DOCUMENT_CACHE
        self.corpus_sha256 = corpus_hash(self.docs)
        self.corpus = [tokens(d["title"] + " " + d["text"]) for d in self.docs]
        self.average = max(1, sum(map(len, self.corpus)) / len(self.corpus))
        self.embedding_failure = None

    def bm25(self, query):
        results = []
        for doc, words in zip(self.docs, self.corpus):
            counts, score = Counter(words), 0.0
            for term in set(tokens(query)):
                df = sum(term in item for item in self.corpus)
                idf = math.log(1 + (len(self.corpus) - df + 0.5) / (df + 0.5))
                tf = counts[term]
                score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * len(words) / self.average))
            if score > 0:
                results.append((doc["id"], round(score, 3)))
        return sorted(results, key=lambda row: (-row[1], row[0]))

    def semantic(self, query):
        if self.embedding_failure:
            raise EmbeddingError(self.embedding_failure)
        if self.embedder is None:
            self.embedder = GeminiEmbedder()
        provider = self.embedder
        key = (provider.cache_key, provider.model, provider.dimensions, self.corpus_sha256)
        def build():
            vectors = provider.embed([d["title"] + "\n" + d["text"] for d in self.docs], "RETRIEVAL_DOCUMENT")
            return normalize_vectors(vectors, len(self.docs), provider.dimensions)
        vectors = self.cache.get_or_create(key, build)
        query_vector = normalize_vectors(provider.embed([query], "RETRIEVAL_QUERY"), 1, provider.dimensions)[0]
        ranked = [(doc["id"], max(-1.0, min(1.0, sum(a*b for a, b in zip(vector, query_vector))))) for doc, vector in zip(self.docs, vectors)]
        return sorted(ranked, key=lambda row: (-row[1], row[0]))

    def search(self, query, k=3, allow_fallback=True):
        if not isinstance(query, str) or not query.strip() or len(query) > 4000 or type(k) is not int or not 1 <= k <= 10:
            raise ValueError("Provide a 1–4000 character query and k between 1 and 10")
        started = time.monotonic()
        lexical, dense = self.bm25(query), []
        effective, fallback = self.mode, None
        if self.mode != "bm25":
            try:
                dense = self.semantic(query)
            except EmbeddingError as exc:
                self.embedding_failure = fallback = exc.code
                if not allow_fallback:
                    raise
                effective = "bm25"
        ranked = lexical if effective == "bm25" else dense if effective == "semantic" else reciprocal_rank_fusion([[i for i, _ in lexical], [i for i, _ in dense]])
        meta = {"requested_mode": self.mode, "effective_mode": effective, "fallback": fallback,
                "embedding_model": getattr(self.embedder, "model", None) if dense else None,
                "dimensions": getattr(self.embedder, "dimensions", None) if dense else None,
                "corpus_sha256": self.corpus_sha256, "rrf_k": RRF_K if effective == "hybrid" else None,
                "duration_ms": round((time.monotonic() - started) * 1000, 3)}
        lexical_ranks = {identifier: rank for rank, (identifier, _) in enumerate(lexical, 1)}
        dense_ranks = {identifier: rank for rank, (identifier, _) in enumerate(dense, 1)}
        docs = {d["id"]: d for d in self.docs}
        return [{**docs[identifier], "score": score,
                 "retrieval": {**meta, "bm25_rank": lexical_ranks.get(identifier), "semantic_rank": dense_ranks.get(identifier)}}
                for identifier, score in ranked[:k]], meta
