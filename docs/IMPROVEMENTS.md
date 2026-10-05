# Improvement roadmap

The most valuable next steps for an AI/ML portfolio are measurable agent quality and reliable execution.

| Priority | Improvement | Why it matters | Acceptance evidence |
| --- | --- | --- | --- |
| 1 | Held-out live-model evaluation | Demo routing does not measure LLM quality | 50–100 labeled cases, held-out test split, and reported tool-selection accuracy, groundedness, abstention, latency, and tokens |
| 2 | Compare BM25 and hybrid retrieval | Lexical matching can miss synonyms | Recall@3 and latency compared using the same corpus and labeled queries |
| 3 | Per-step checkpoints and resumption | Provider failures currently lose an in-flight trace | Resume an interrupted investigation without replaying committed actions |
| 4 | Streaming progress | Live requests can feel stalled | Browser receives step events, failure state, and supports reconnecting |
| 5 | Authenticated production API | Current server is local and unauthenticated | Tenant-scoped authorization, authentication, rate limits, request deadlines, and a production web server |
| 6 | Sandbox ticket connector | Local records do not demonstrate external integration | Approved proposals create one sandbox ticket; replay does not duplicate writes |
| 7 | Broader groundedness and injection evaluation | The checker catches only specific patterns | Adversarial cases and contradictory evidence evaluated with explicit failure reporting |

## Included improvements

- SQLite connections close deterministically after commit/rollback.
- The README documents setup, architecture, configuration, tests, API, and limitations.
- CI has read-only repository permissions and audits tracked publication contents.
- Publication excludes credentials, runtime databases, local screenshots, and private tool configuration.
- Setup examples use repository-relative paths instead of personal filesystem paths.

These improvements do not establish production readiness or measured live-model accuracy. Credentials remain local.
