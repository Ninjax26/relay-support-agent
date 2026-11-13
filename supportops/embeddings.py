"""Optional Gemini text embeddings. No credentials or vectors are persisted."""
import hashlib
import json
import math
import os
import urllib.error
import urllib.request

MODEL = "gemini-embedding-001"
DIMENSIONS = 768


class EmbeddingError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Semantic retrieval unavailable ({code}); provider details are not exposed.")


def normalize_vectors(vectors, count, dimensions):
    if not isinstance(vectors, list) or len(vectors) != count:
        raise EmbeddingError("invalid_vectors")
    normalized = []
    for vector in vectors:
        if not isinstance(vector, (list, tuple)) or len(vector) != dimensions:
            raise EmbeddingError("invalid_vectors")
        try:
            valid = all(type(v) in (int, float) and math.isfinite(v) for v in vector)
            norm = math.hypot(*vector) if valid else 0
        except (OverflowError, ValueError, TypeError):
            raise EmbeddingError("invalid_vectors") from None
        if not norm or not math.isfinite(norm):
            raise EmbeddingError("invalid_vectors")
        normalized.append(tuple(v / norm for v in vector))
    return normalized


class GeminiEmbedder:
    model = MODEL
    dimensions = DIMENSIONS

    def __init__(self, key=None):
        self.key = (key if key is not None else os.environ.get("GEMINI_API_KEY", "")).strip()
        if not self.key:
            raise EmbeddingError("missing_key")
        # Scope cached documents to this provider identity, without exposing keys.
        self.cache_key = ("gemini-v1beta-retrieval-v1", self.model, self.dimensions,
                          hashlib.sha256(self.key.encode()).hexdigest())

    def embed(self, texts, task):
        if task not in {"RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"} or not texts or len(texts) > 100 or any(not isinstance(t, str) or not t.strip() or len(t) > 20000 for t in texts):
            raise EmbeddingError("invalid_input")
        requests = [{"model": "models/" + self.model, "content": {"parts": [{"text": text}]},
                     "taskType": task, "outputDimensionality": self.dimensions} for text in texts]
        request = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models/" + self.model + ":batchEmbedContents",
            data=json.dumps({"requests": requests}).encode(),
            headers={"x-goog-api-key": self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise EmbeddingError("invalid_vectors")
            payload = json.loads(raw)
            vectors = [row["values"] for row in payload["embeddings"]]
            return normalize_vectors(vectors, len(texts), self.dimensions)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise EmbeddingError("quota" if code == 429 else "access" if code in {401, 403, 404} else "provider_http") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise EmbeddingError("connection") from None
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise EmbeddingError("invalid_vectors") from None
