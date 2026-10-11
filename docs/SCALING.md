# Persistent background investigations

Relay now submits work to a SQLite-backed queue and returns HTTP 202 immediately. Configurable workers investigate in the background. Browser progress polling does not keep the submission request open, and the most recently selected run is restored after a page reload.

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
- **Retry starts a new run** linked by `retry_of`. It does not resume a model conversation or replay a previous approval. The previous trace is retained.
- Provider state and private model reasoning are not saved for resumption. No automatic crash replay is performed.

Approval is possible only for `awaiting_approval` runs. Existing repeated approvals still return the same local ticket. Queued, running, failed, and interrupted runs cannot create tickets.

## API changes

### Submit

`POST /api/runs` now returns **202** with a queued run instead of 201 with a finished answer.

```json
{"message":"API timeout","account_id":"acct_demo","mode":"demo","request_id":"client-generated-unique-id"}
```

Use the same `request_id` when retrying the same HTTP submission. The server returns the same run. Reusing the key for different input returns 400. If omitted, the server generates a key and cannot deduplicate a later client retry.

### Observe

`GET /api/runs/{id}` returns the current snapshot. Check `status` and `version`; the answer is null until completion. The UI polls every 1.5 seconds, or 5 seconds in a hidden tab. This is polling, not SSE streaming.

### History

`GET /api/runs?limit=20&before=<run-id>` returns `{ "items": [...], "next_cursor": "..." }`. Items contain compact metadata. Fetch an individual run for full evidence and trace. Limits are 1–100; ordering uses timestamp and ID for stable cursor pagination.

### Retry

`POST /api/runs/{id}/retry` with a new `request_id` creates a fresh investigation only for a failed/interrupted parent. Repeating that retry key returns the same child.

### Health

`GET /api/health` provides queue-state counts, configured worker count/capacity, and ticket count without customer messages or credentials. It is a local diagnostics endpoint, not an authenticated production monitoring service.

## Load check

```sh
python3 scripts/benchmark_jobs.py --jobs 50 --workers 4
```

One local deterministic run completed 50/50 jobs with four workers, all step traces saved, and zero unapproved tickets. Wall time was 0.454 seconds and enqueue p95 was 3.213 ms on the development machine. These figures exclude model and HTTP latency and are not a production capacity or live-AI throughput claim. Run the script on your own machine to reproduce the measurement.

## Remaining production requirements

This improves concurrency on a local machine. It does not turn the standard-library HTTP server or SQLite into a distributed multi-tenant service. Before internet deployment: add a production server, authentication and tenant authorization, provider-aware rate limits, hard request deadlines, log/trace retention, and a managed database/queue. Do not share SQLite over network filesystems. Different server processes sharing a database share queued work and must use compatible provider settings; independent workspaces should use separate databases.
