# Execution workflow

Relay processes support investigations through a persistent queue, a bounded tool-calling loop, and a separate approval transaction.

## Investigation lifecycle

1. **Submit:** The API validates the issue, account, and mode; stores a queued job; and returns HTTP 202 with the investigation ID. Repeated submissions with the same request ID and input return the same job.
2. **Claim:** A background worker claims an eligible job under a renewable lease. Live jobs are assigned to workers configured for the selected provider.
3. **Plan:** The agent records an operational checklist using `update_plan`.
4. **Retrieve and inspect:** `search_knowledge` retrieves policy documents using BM25. Account and service tools return scoped fixture data. Observations are returned to the model for subsequent tool selection.
5. **Verify:** `verify_case` checks the proposed reply, evidence references, action-specific requirements, identifiers, and limited prohibited action claims. Failed checks return to the agent for revision.
6. **Complete:** `complete_case` accepts only the exact verified proposal after the latest evidence or plan change. The investigation enters `awaiting_approval`.
7. **Review:** A human approves or rejects the proposal. Approval creates one local review ticket; repeated approvals return the same ticket.

Each completed tool call checkpoints its trace, evidence, plan, verification, token totals, and duration. The browser polls the saved snapshot and restores the selected investigation after reload.

## Tool boundaries

| Tool | Responsibility |
| --- | --- |
| `update_plan` | Record or revise the investigation checklist |
| `search_knowledge` | Retrieve relevant support policies |
| `lookup_account` | Read the account scoped to the investigation |
| `check_service` | Read supported service status |
| `verify_case` | Check a proposed reply against evidence and policy requirements |
| `complete_case` | Finalize the verified proposal for human review |

The dispatcher validates tool names and argument schemas. The model has no shell, arbitrary SQL, payment mutation, or account mutation tool. Ticket creation occurs through the separate human decision API.

## Execution modes

Live mode uses Gemini or OpenAI to select tools based on observations. Demo mode uses a deterministic planner with the same tool dispatcher and approval boundary. Account, payment, incident, and policy data are supplied fixtures.

## Failure handling

Provider failures preserve completed steps and mark the run failed. A worker crash leaves the run interrupted after lease expiration. Explicit retry starts a new linked investigation; it does not restore the previous model conversation or replay an approval.

The agent permits at most 16 tool calls and checks its wall-clock budget between calls. Verification is mechanical and does not establish semantic support for every generated claim. Retrieval is lexical BM25. For worker configuration and recovery details, see [job processing](SCALING.md).
