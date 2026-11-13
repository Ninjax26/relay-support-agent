# Resume a live investigation

Relay can continue a failed or interrupted **live** investigation from a privately saved tool-step boundary. Resume keeps the same investigation ID, conversation, evidence, plan, verification and remaining budgets. Demo investigations keep their existing offline fresh-run retry behavior.

## Use the controls

1. Start live mode with the provider key in a hidden local prompt. To retain the ticket test service, use:

   ```sh
   python3 scripts/run_sandbox.py --live --provider gemini
   ```

2. Investigate an issue. A handled provider failure marks the investigation failed. After a worker crash, its job becomes interrupted when its ninety-second lease expires and an active worker scans the queue.
3. Select **Resume investigation** when a compatible saved live conversation is available. The existing investigation is queued again. The model receives the prior completed tool exchanges and chooses its next call.
4. Review the completed proposal before approving it. Resume does not approve or deliver a ticket.

**Retry as a new investigation** remains a separate choice: it creates a new linked run and begins from the original input. **Retry ticket delivery** operates only on an already approved ticket and does not involve the model.

The Resume button reflects the last saved checkpoint's availability. A later configuration change, exhausted in-flight budget, damaged checkpoint or unavailable key can still make the request fail with a specific message. Restore the original configuration or start a new investigation.

## What is saved

The backend keeps a versioned private checkpoint in a separate SQLite table. It contains the provider conversation, complete function-call results, tool state, trace, usage and budget counters, and compatibility fingerprints. The Gemini adapter preserves its full assistant message, including opaque tool-call metadata; the OpenAI adapter preserves the response output items needed to replay the conversation. This is local conversation replay rather than a dependency on provider-side conversation storage. See the provider's [conversation-state guide](https://platform.openai.com/docs/guides/conversation-state) and [Gemini compatibility documentation](https://ai.google.dev/gemini-api/docs/openai).

Each completed step appends the tool's output to the conversation **before** the private checkpoint and public progress are committed in one transaction. Recovery restores tool state directly. It does not call the completed tools again to rebuild state. The saved exact-proposal verification boundary remains enforced.

Model requests are reserved durably before they are sent. A provider failure saves its consumed request count and measured active time while retaining the last complete exchange. A crash can leave one request or read tool uncommitted; recovery discards that unfinished exchange and may request it again. Its reserved model request still counts. No model tool performs an external ticket, approval, payment or account write.

If `complete_case` was saved but the worker crashed before the final run status committed, resume restores that draft to `awaiting_approval` without another provider request. Once final completion commits, its private checkpoint is deleted; the normal public investigation trace remains.

## Compatibility and budgets

Resume requires the same original issue and account, provider, configured model name, retrieval mode, prompt, loaded engine fingerprint and fixture data. Credentials are excluded from the fingerprint so a key can be rotated for the same provider. A queued resume revalidates its checkpoint when a worker starts; changing configuration after queueing cannot send old context to a different model.

The original limits continue across resumes:

- **16 local tool calls** across the investigation.
- **16 planner requests**, including failed or interrupted requests. Gemini's internal retry of a single HTTP request remains separate and bounded as before.
- **180 seconds of measured active work**, checked between calls. Settled time waiting for a user to resume is excluded.

An in-flight crash has an ambiguous elapsed interval. Recovery conservatively charges the wall-clock interval since the saved request reservation, capped at 180 seconds, because it cannot distinguish completed work from offline wait. A long delay after such a crash can therefore exhaust the remaining budget. This is intentional; it does not reset or grant extra budget. Waiting after a settled provider failure does not incur that charge. Provider-reported tokens from a response lost in a crash may be unknown. Counters and latency are not a complete provider billing ledger.

The time check is not hard cancellation of a provider or embedding request already in progress. Interrupted reads may repeat, but committed tool results, human approval and ticket delivery are not replayed by conversation resume.

## Queue and concurrency

`POST /api/runs/{id}/resume` accepts a `request_id` and returns HTTP 202 with the same investigation ID. Repeating that request ID returns the investigation's current state, including after completion. Reusing it for another run is rejected. No history, checkpoint, provider or account override is accepted from the browser.

Only failed/interrupted live jobs with a usable checkpoint can resume. Active, completed, approved, rejected and demo runs cannot resume. Queue capacity still applies. Resuming increments `resume_count`; subsequent tool steps keep their original numbering. Every claim has a distinct owner token, including claims within the same server process. Heartbeats refresh only active unexpired ownership. A previous attempt cannot overwrite public or private state after a newer claim.

Public responses include `resume_available`, `resume_count` when used, and `model_requests`. Raw conversations, compatibility bindings and opaque provider metadata do not appear in run/history responses or downloaded investigation JSON. The resume endpoint loads the checkpoint from the server database rather than trusting a client-supplied state object.

## Local data handling and validation

Checkpoints are bounded to two megabytes and versioned. A checksum detects accidental corruption; it is not authentication against someone who can edit the database. The database is local plaintext and contains customer input and tool observations. Keep it private. Planner/API-key objects are never serialized. SQLite files and sidecars are ignored by Git and blocked by the publication allowlist. This is logical separation from the browser, not encryption at rest. Failed or interrupted checkpoints remain until completion; automated retention/cleanup is not implemented.

Run recovery tests and the full project suite:

```sh
python3 -m unittest discover -s tests -p 'test_resume*.py' -q
python3 -m unittest discover -s tests -q
```

Tests use controlled provider responses to exercise exact opaque-metadata preservation, saved-state reopening, cumulative budgets, provider/request/configuration mismatch, atomic rollback, same-process stale-worker races, saved completion recovery, HTTP idempotency, capacity guards, corruption and public-data exclusion. They do not establish real Gemini/OpenAI recovery quality or token savings; that requires credentials and separate live failure-injection validation. This feature does not add streaming or cancellation.
