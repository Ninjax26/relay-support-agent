# Approved-ticket delivery

Relay can deliver approved review proposals over HTTP to an isolated local ticket service. This service has its own SQLite database, authenticated API, and persistent idempotency records. It is a test integration, not a Zendesk or Jira adapter.

## Start the integrated workspace

Stop the existing Relay process on port 8012, then run:

```sh
python3 scripts/run_sandbox.py
```

Open **http://127.0.0.1:8012**. The launcher starts two loopback HTTP servers in one Python process: Relay on 8012 and the ticket service on 8014. They exchange real HTTP requests and persist separately. A random shared credential is generated in memory; it is never printed or saved. No third-party account, API key, or additional package is required for demo investigations and ticket delivery.

To also enable model-driven investigations with a hidden local provider-key prompt:

```sh
python3 scripts/run_sandbox.py --live --provider gemini
```

Generation/embedding usage still follows the selected provider's access, quota and pricing. Ticket delivery does not require a model call. The launcher accepts `--port` and `--ticket-port` for distinct custom ports.

`python3 server.py` retains local-only approval unless a connector is explicitly configured. Advanced embedding into another local process may supply RELAY_TICKET_URL and RELAY_TICKET_TOKEN; the only supported URL format is `http://127.0.0.1:PORT`, with no path, credentials, query or fragment. The shared token must contain 16–200 URL-safe characters. Both HTTP servers and the same credential must be configured; setting a URL alone does not start a ticket service. Environment example files are not automatically loaded.

## Workflow

1. Investigate and verify a proposal using the existing read-only tools.
2. A person inspects the draft and clicks **Approve review ticket**. The interface explains whether delivery to the local test service is enabled.
3. One database transaction records approval, creates a local `TKT-...` review ticket, and writes its delivery job. If that transaction fails, none of those changes commit.
4. A background delivery worker claims the job under a thirty-second lease and posts only the approved ticket fields to the test service.
5. The test service persists one `SBX-...` ticket for the request's stable idempotency key. Identical replays return that ticket. A changed payload with the same key returns a conflict.
6. Relay records the external ticket ID and delivery state. The interface polls these changes and distinguishes pending, delivered and failed delivery from approval.

The LLM cannot approve, send, or retry a ticket: delivery is absent from its tool definitions. Rejected and unapproved proposals do not create delivery jobs. Previously approved local-only tickets are **not backfilled** when a connector is enabled later. Only a new explicit approval made with the connector configured queues delivery.

## Reliability and recovery

Approval and local queueing are atomic. The remote HTTP write is not part of that transaction. Delivery is **at least once**; duplicate prevention depends on the test service retaining its idempotency records.

A timeout or crash after remote acceptance can leave the outcome ambiguous. Relay retries the exact saved payload and key; the service returns the existing remote ticket. A lease that expires allows a new worker to claim the job. Every attempt has a distinct owner token; stale workers cannot overwrite a newer attempt or acknowledge an expired lease.

Temporary connection failures, quota errors, invalid success responses and selected HTTP server errors receive up to five automatic claims per retry cycle. Backoff starts at one second and doubles, with Retry-After capped at sixty seconds. Authentication, conflict and invalid request errors stop immediately. Crashed claims also count toward the limit. A crash during the fifth attempt becomes a failed delivery after lease expiration; the remote ticket may already exist.

**Retry ticket delivery** starts a new retry cycle on the same investigation, local ticket, destination, approved payload and idempotency key. It preserves the lifetime attempt count. It does not repeat the agent investigation. Repeated retry requests while already pending leave the pending job unchanged; sending or delivered jobs cannot be manually retried.

Pending jobs survive app restart. Workers claim only jobs bound to their configured destination. Changing the configured port cannot silently redirect an existing job. Restore its original destination to deliver or retry it. Keep the sandbox database when restarting: deleting remote idempotency records can allow replayed requests to create another test ticket.

## Data and interface contracts

- `POST /api/runs/{id}/decision` accepts approve/reject; the destination comes from server configuration, never the request body.
- `GET /api/runs/{id}` includes `ticket_delivery` when an outbox record exists: state, attempts, cycle attempts, next attempt, external ID, sanitized error code, and update time. Credentials, destination and delivery payload are excluded.
- `POST /api/runs/{id}/ticket/retry` retries an existing failed delivery. The existing investigation `/retry` endpoint remains separate.
- `/api/config` reports `ticket_connector` as `sandbox` or `local_only`; `/api/health` includes delivery counts.

The service accepts authenticated `POST /tickets` requests with a stable `Idempotency-Key` and JSON containing only local ticket ID, run ID, action, summary, reason and citation IDs. It returns HTTP 201 for a new ticket or 200 for an identical replay. Authenticated `GET /health` reports ticket count. Requests with foreign Host/Origin or missing credentials are rejected; provider bodies and credentials are not returned to the Relay browser. The client makes direct loopback connections, does not use environment proxies, and does not follow redirects.

`data/supportops.sqlite3` contains approval/outbox records. `data/ticket-sandbox.sqlite3` contains test tickets. Both files and their SQLite sidecars are ignored by Git. Downloaded investigation JSON contains delivery status but no connector credential.

## Validation and limits

Tests exercise actual loopback HTTP delivery, concurrent approval/claims, approval rollback, rejection, local-only behavior, repeated payload conflicts, authentication failure, backoff/exhaustion, manual recovery, restart after a lost acknowledgment, destination binding and stale-worker fencing. Run:

```sh
python3 -m unittest discover -s tests -q
```

This is a local integration environment with a single delivery worker per Relay server. It does not establish production throughput, multi-user authentication, third-party API compatibility, or production exactly-once delivery. No payment, refund, account unlock, cancellation, or customer notification is executed. A real helpdesk adapter requires its sandbox account, scoped credentials, API-specific mapping and a separate validation pass.
