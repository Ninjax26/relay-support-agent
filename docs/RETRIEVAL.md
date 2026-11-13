# Retrieval strategies and evaluation

Relay supports three knowledge-search strategies. Account and service observations continue to use scoped read tools; they are not embedded.

| Strategy | Ranking | External dependency |
| --- | --- | --- |
| `bm25` | Keyword relevance, deterministic ID tie-breaking | None; the default |
| `semantic` | Cosine similarity of normalized Gemini text embeddings | Gemini API key and embedding-model access |
| `hybrid` | Reciprocal rank fusion of BM25 and semantic ranks, with constant 60 | Same embedding access |

The embedding adapter targets **gemini-embedding-001**, 768 dimensions, with `RETRIEVAL_DOCUMENT` and `RETRIEVAL_QUERY` task types. It uses Google's synchronous `batchEmbedContents` REST endpoint. Embeddings are validated for response count, dimension, finite values, and nonzero norm, then normalized before comparison. This adapter intentionally targets Embedding 001: Embedding 2 has different task-formatting requirements and is not a drop-in configuration change. See Google's [embedding guide](https://ai.google.dev/gemini-api/docs/embeddings) and [REST reference](https://ai.google.dev/api/embeddings).

## Configure live search

Demo investigations always use offline BM25, including when live retrieval is configured as hybrid. Existing tool arguments and evidence IDs remain unchanged.

For Gemini generation and hybrid retrieval, start the local server with a hidden key prompt:

```sh
RELAY_RETRIEVAL_MODE=hybrid PORT=8012 python3 run_live.py --provider gemini
```

Stop any existing server on that port first. Choose **Live** for the investigation. The same Gemini key is used for generation and embeddings. Generation through OpenAI still needs a separately configured server-side GEMINI_API_KEY for semantic retrieval; the OpenAI key does not grant Gemini embedding access. Environment templates are examples and are not automatically loaded.

Semantic/hybrid search sends the search query and knowledge text to Google. It never embeds account/payment/service records. The generation provider still receives the tool observations as documented by live mode. Provider quota, model availability and account pricing apply; this project does not guarantee free embedding access.

A process-wide memory cache holds at most four document-vector indexes, keyed by provider identity, model, dimensions, and corpus content. Concurrent workers share a cold build. Indexes disappear on restart. Query vectors are never cached and neither vectors nor credentials are written to disk. Ordinary investigation traces still retain search arguments and retrieved evidence, as they did before this feature.

If embedding access fails, runtime search falls back to BM25. Every result and saved search event identifies `requested_mode`, `effective_mode`, and a sanitized `fallback` code such as `missing_key`, `quota`, `connection`, or `invalid_vectors`. A failure suppresses further embedding requests for that investigation's retriever. Fallback is never presented as semantic success. Inspect `retrieval` in run snapshots or the search result in the tool trace. `/api/config` reports the configured live search mode.

## Compare strategies

The retrieval dataset has **24 synthetic English queries**, with fixed 12-query development and 12-query reserved test splits. It covers six playbooks, with keyword and paraphrased queries. Labels are authored relevance judgments, not independently validated customer outcomes. Do not tune on the reserved split.

Offline development baseline:

```sh
python3 scripts/benchmark_retrieval.py --modes bm25 --split dev --output reports/retrieval-bm25-dev.json
```

Actual provider comparison using a hidden local key prompt:

```sh
python3 scripts/benchmark_retrieval.py --modes bm25 semantic hybrid --split dev --prompt-key --output reports/retrieval-comparison-dev.json
```

After freezing the embedding model, fusion configuration and corpus, change `--split dev` to `--split test` and use a new output path. A public split remains a holdout only while it is excluded from tuning. No key is printed or saved. Reports never overwrite existing files and remain local in ignored `reports/`.

| Metric | Meaning |
| --- | --- |
| Recall@k | Fraction of labeled relevant playbooks found in the first k results |
| MRR@k | Reciprocal position of the first relevant result, or zero if absent |
| nDCG@k | Rank-discounted binary relevance, divided by ideal ranking gain |
| Errors/completed | Provider failures remain in the denominator with zero metric credit |
| Mean/p95 wall time | Retrieval time; the first semantic strategy includes a cold document build and later strategies may reuse it |

The comparison is strict: **no BM25 fallback is scored as semantic or hybrid**. Errors use sanitized codes and make the command exit unsuccessfully after writing the report. A retriever stops requesting embeddings after its first provider failure, avoiding repeated requests for subsequent cases. The benchmark records dataset/corpus hashes, engine/benchmark hashes, model, dimension, fusion constant, selected split, UTC timestamp, actual result IDs, and per-case metrics. Query vectors are never cached. Generation tokens and embedding monetary cost are not measured.

## Interpretation and limits

These retrieval metrics measure whether a labeled document was found. They do not measure final-answer groundedness, correct action selection, prompt-injection resistance, or production performance. Evaluate answer quality separately with the [agent quality suite](EVALUATION.md), whose reports also fingerprint retrieval configuration and code.

The corpus has only six documents. Semantic search always returns nearest neighbors, including weak matches; there is no validated abstention threshold or reranker. Hybrid fusion and 768 dimensions are initial engineering choices, not tuned optima. This small-corpus implementation uses an in-memory linear vector scan with a 100-document limit and per-call network timeout. A larger corpus needs ingestion/chunking, durable versioned indexes and a measured approximate-nearest-neighbor store. BM25 stays the default until real development comparison and human answer review support changing it.
