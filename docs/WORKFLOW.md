# Understand the workflow

## Example: “I was charged twice”

1. **Input:** You select Northstar Labs and enter the issue. The backend validates input and persists a queued job, returning an ID immediately. Background workers execute the investigation.
2. **Plan and retrieve:** The agent records a short operational checklist with `update_plan`.  `search_knowledge` ranks policy documents with BM25. Each result has an ID, title, text, and score. This supplies relevant context rather than relying on model memory.
3. **Observe:** `lookup_account` reads only the account selected for this run. Two settled payments share invoice INV-204. In live mode, the model sees these observations and decides its next tool call.
4. **Revise, verify, complete:** The agent may revise its plan, then calls `verify_case`. Missing evidence is returned as feedback; the live model can search again or revise. `complete_case` accepts only the exact draft that passed after the latest evidence/plan change.  `complete_case` receives a customer reply, citations, review type, and reason. The dispatcher rejects unknown citation IDs and unsupported actions.
5. **Checkpoint:** SQLite saves each completed tool step and finally marks the run `awaiting_approval`. The UI polls saved progress; failures keep the existing trace.
6. **Human review:** You inspect the facts and approve or reject. Approval creates one local billing-review ticket. Repeated approval returns the same ticket. It never issues an actual refund.

## Why this is agentic in live mode

A conventional chatbot generates a reply directly. This live agent selects a tool, observes its result, and selects the next tool. Different issues need different observations: a timeout needs service status; an export failure needs plan eligibility and service status. `complete_case` ends the loop. The deterministic demo imitates these workflows for repeatable demonstrations; it is not autonomous LLM reasoning.

## Four concepts to explain in an interview

**Tool calling:** The LLM proposes a structured function invocation. Python, not the model, validates and executes it. A model never receives arbitrary shell or SQL access.

**Retrieval grounding:** BM25 matches issue terms against documents. The retrieved passages become context for answer generation. This is a lexical RAG pipeline in live mode. BM25 is fast and explainable for this small corpus but weak on synonyms and semantic similarity. Embeddings and hybrid search are useful experiments, not implemented features.

**Human-in-the-loop:** The proposal and execution have different API paths. The LLM can only propose. A separate human review request creates a ticket. The tool schema has no payment or account mutation tool.

**Evaluation:** Tests establish control flow and persistence properties. Deterministic scenarios verify routing and required evidence. To measure actual ML quality, run a held-out live dataset and grade factual support, tool choice, retrieval recall, resolution quality, and abstention. A 100% demo pass rate says nothing about live LLM accuracy.

## Limits and tradeoffs

- Citation existence is checked; entailment is not. A live model can still write an unsupported claim. Human review and held-out groundedness evaluation are necessary.
- Prompts treat customer messages/documents as untrusted. This instruction alone is not a proven injection defense. Tool allowlisting and approval separation limit what the model can do even if it follows malicious text.
- Completed tool calls are checkpointed. Worker crashes preserve those steps; after lease expiry the run becomes interrupted. Explicit retry starts a new linked run instead of resuming the conversation.
- Live investigations run in a persistent background queue. The browser polls progress and reconnects after reload. Push streaming and full conversation resumption remain future improvements.
- This small app uses Python's standard HTTP server to minimize setup. A public service needs a production framework/server and authentication.
- One agent is intentional. Multiple agents add routing and coordination cost; introduce them only if measured task quality or specialization justifies it.

## A 60-second explanation

“I built Relay, an evidence-driven support operations agent. A live LLM chooses read-only tools to retrieve policy documents and inspect scoped account or service facts. The backend validates every tool call, enforces a sixteen-call budget, and requires citations to retrieved evidence. It saves a structured investigation trace in SQLite. Customer-impacting decisions go through a separate human review path; approval creates an idempotent local ticket. I also built a deterministic demo and regression checks so the workflow can be tested without paid API calls. The next experiment is comparing lexical and hybrid retrieval on a labeled dataset and measuring live answer groundedness.”
