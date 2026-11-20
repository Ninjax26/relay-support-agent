# Persistent background investigations

Relay returns HTTP 202 when it queues an investigation. The local server stores runs in SQLite and streams saved progress with server-sent events. The hosted app stores runs in PostgreSQL and polls for progress.

## Run locally

```sh
RELAY_WORKERS=4 RELAY_QUEUE_CAPACITY=100 PORT=8001 python3 run_live.py
```

- `RELAY_WORKERS`: 1–16 per server process, default 2. Increasing workers also increases provider request concurrency; it does not increase your model quota.
- `RELAY_QUEUE_CAPACITY`: 1–5000 queued plus running jobs, default 100. Full queues reject new submissions with HTTP 429.
- `RELAY_DB_PATH`: optional SQLite path, default `data/supportops.sqlite3`. Use a separate path for independent local workspaces.

SQLite WAL and indexed queue/history queries reduce contention and avoid loading every completed trace for history lists. Transactions claim one job at a time. Claims use a worker owner and a 90-second lease renewed every 10 seconds. A stale owner cannot save a completed proposal after losing its lease.

## Failure recovery

- Queued jobs are persistent and can be picked up after a restart.
- Every completed tool call saves its trace, evidence, plan, verification, token totals, and duration.
- A normal provider failure marks the run `failed`; saved steps remain inspectable.
- A crashed worker's job becomes `interrupted` after its lease expires and a worker scans the queue. This may take around 90 seconds. Heartbeats do not revive expired leases.
- **Retry starts a new run** linked by `retry_of`. It begins a fresh conversation and does not replay a previous approval; the separate resume endpoint continues compatible saved live work on the same ID. The previous trace is retained.
- Live provider exchanges and tool state are saved in a private checkpoint table. Explicit resume continues a compatible failed/interrupted live run; no automatic crash replay or approval replay is performed. See [conversation recovery](RECOVERY.md).

Approval is possible only for `awaiting_approval` runs. Existing repeated approvals still return the same local ticket. Queued, running, failed, and interrupted runs cannot create tickets.

## API reference

### Submit

`POST /api/runs` returns **202** with the queued investigation.

```json
{"message":"API timeout","account_id":"acct_demo","mode":"demo","request_id":"client-generated-unique-id"}
```

Use the same `request_id` when retrying the same HTTP submission. The server returns the same run. Reusing the key for different input returns 400. If omitted, the server generates a key and cannot deduplicate a later client retry.

### Observe

`GET /api/runs/{id}` returns the current snapshot. Check `status` and `version`; the answer is null until completion. The local UI uses server-sent events with a polling fallback. The hosted UI polls every 1.5 seconds, slowing to five seconds in a hidden tab.

### History

`GET /api/runs?limit=20&before=<run-id>` returns `{ "items": [...], "next_cursor": "..." }`. Items contain compact metadata. Fetch an individual run for full evidence and trace. Limits are 1–100; ordering uses timestamp and ID for stable cursor pagination.

### Retry

`POST /api/runs/{id}/retry` with a new `request_id` creates a fresh investigation only for a failed/interrupted parent. Repeating that retry key returns the same child.

### Health

`GET /api/health` reports database readiness on the hosted app. The local server reports worker, queue, and ticket counts.

## Load check

```sh
python3 scripts/benchmark_jobs.py --jobs 50 --workers 4
```

The command runs deterministic investigations against a temporary database and reports completion count, enqueue latency, elapsed time, and checkpoint coverage. It does not call model providers or include HTTP latency.

## Hosted execution

On Vercel, the Python function uses PostgreSQL for sessions, workspace policies, jobs, and checkpoints. The signed-in browser starts processing a queued run and polls for progress. Opening a queued case can start processing if the previous browser request ended before a worker claimed it. Worker leases fence stale writers across function instances.

The local server uses a loopback listener and SQLite by default. Keep SQLite on local storage. Live jobs record their selected provider and only compatible workers claim them.

### Resume a live conversation

`POST /api/runs/{id}/resume` with a stable `request_id` requeues compatible failed/interrupted live work on its existing ID. Original tool/model-request/time budgets apply, and every worker claim has a distinct lease owner. Private conversation state and public progress commit atomically; only public progress is returned by the API. See [recovery](RECOVERY.md) for compatibility, privacy and ambiguous in-flight behavior.

### Streaming and cancellation

The local `GET /api/runs/{id}/events` route streams versioned progress snapshots with SSE event IDs. The server checks the stored run state and closes idle connections after 25 seconds; browsers reconnect or use the polling fallback. `POST /api/runs/{id}/cancel` immediately cancels queued or interrupted work. For a running job it durably records `cancel_requested`, fences later checkpoints, and the worker marks the run cancelled at the next boundary. A provider request already in flight is allowed to return or hit its transport timeout before cancellation completes. Cancelled runs cannot be approved or resumed. This is cooperative cancellation, not a hard provider abort.
