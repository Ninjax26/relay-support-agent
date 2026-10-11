# Resume and demo guide

## Project title

**Relay — Tool-Using Support Operations Agent**

Stack: Python, Gemini API / OpenAI Responses API, BM25 retrieval, SQLite, JavaScript, HTML/CSS, unittest, GitHub Actions.

## Resume bullets supported by this implementation

- Built a support investigation agent with model-selected tool calls, BM25 knowledge retrieval, scoped account/service tools, and validated evidence citations.
- Added a persistent SQLite job queue with background workers, saved tool-step checkpoints, idempotent submissions, paginated history, and retry for failed investigations.
- Implemented a human approval workflow with transactional SQLite persistence and idempotent local review-ticket creation; prevented agent access to payment/account mutation tools.
- Added observable tool traces, token usage accounting, bounded execution, explicit plan revisions, a verification-and-revision loop, deterministic regression evaluation, and fake-provider tests for tool-loop recovery and failure handling.

Only use the first bullet after you configure and demonstrate live mode. Until then say “Built a support agent prototype with a deterministic demo and an optional LLM tool-calling adapter.” Do not claim measured business impact, live accuracy, production deployment, vector search, multi-agent architecture, or real customer integrations.

## Five-minute demo

1. Run the app and show duplicate-charge investigation. Open the account evidence and KB-101; identify the duplicate invoice.
2. Explain that an agent proposal has not changed payments. Approve the review ticket and show the saved ticket ID.
3. Run API timeouts. Show service-status tool selection, incident ID, and the absence of an invented recovery estimate.
4. Run export failure on Cedar Studio. Explain the difference between healthy service status and missing plan entitlement.
5. Expand tool traces, download JSON, and run unit tests/evals. Explain demo versus live mode clearly.

## Make it stand out for AI/ML roles

The current project is an agent engineering foundation. Add an experiment report to demonstrate ML depth:

- Write 50–100 original labeled cases; keep a test split hidden during prompt development.
- Compare BM25, embeddings, and hybrid retrieval on Recall@3 and latency.
- Compare two live models on correct tool selection, grounded answers, abstention, tokens, and wall-clock latency.
- Evaluate prompt injection and misleading evidence; report failures as well as successes.
- Record the exact dataset size, evaluation definitions, model version, run date, and uncertainty. Put measured numbers on the resume only after running the experiments.

## Likely interview questions

**Why not multiple agents?** A bounded single-agent loop is easier to debug and evaluate. Add specialists only if experiments show a benefit.

**How do you prevent hallucination?** Retrieved evidence and citation validation improve traceability. They do not prove truth; semantic groundedness needs separate evaluation and human review.

**How do you prevent duplicate writes?** The approval transaction locks SQLite and a unique run ID on tickets enforces one local ticket per run. Repeated identical decisions return the saved result.

**What would you change for production?** Authentication, tenant isolation, real scoped connectors, durable step checkpoints, queues, retries, streaming, monitoring, and live evaluation.
