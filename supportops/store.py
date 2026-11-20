"""SQLite run checkpoints and transactional, idempotent review decisions."""
import json
import sqlite3
import uuid
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from .ticketing import validate_destination, validate_payload
from .checkpoints import validate_state, resumable, digest

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data/supportops.sqlite3"


class QueueFull(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class CancelRequested(RuntimeError):
    pass


class Store:
    def __init__(self, path=DEFAULT_DB, daily_live_limit=8, database_url=None):
        self.postgres = None
        if database_url:
            from .database import PostgresDatabase

            self.postgres = PostgresDatabase(database_url)
        self.path = str(path)
        self.daily_live_limit = daily_live_limit
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, run_id TEXT UNIQUE NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (run_id TEXT PRIMARY KEY, state TEXT NOT NULL, owner TEXT, lease_until REAL,
            request_key TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_checkpoints (run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, checksum TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS resume_requests (request_key TEXT PRIMARY KEY, run_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ticket_deliveries (
                ticket_id TEXT PRIMARY KEY, run_id TEXT UNIQUE NOT NULL, destination TEXT NOT NULL,
                idempotency_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, cycle_attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, owner TEXT, lease_until REAL,
                external_id TEXT, error_code TEXT, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS delivery_ready ON ticket_deliveries(destination,state,next_attempt);
            CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, lease_until);
            CREATE INDEX IF NOT EXISTS runs_recent ON runs(created_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS run_owners (run_id TEXT PRIMARY KEY REFERENCES runs(id), user_id TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS owner_runs ON run_owners(user_id,run_id);
            CREATE TABLE IF NOT EXISTS usage_windows (user_id TEXT NOT NULL, mode TEXT NOT NULL, started BIGINT NOT NULL);
            CREATE INDEX IF NOT EXISTS usage_start ON usage_windows(started);
            CREATE TABLE IF NOT EXISTS workspace_policies (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS policies_owner ON workspace_policies(user_id,created_at,id);
            CREATE TABLE IF NOT EXISTS run_policy_snapshots (run_id TEXT PRIMARY KEY REFERENCES runs(id), payload TEXT NOT NULL);""")

    @contextmanager
    def connect(self):
        if self.postgres:
            with self.postgres.connect() as db:
                yield db
            return
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, run):
        run = {**run, "id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat()}
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?, ?, ?)", (run["id"], run["created_at"], json.dumps(run)))
        return run

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("""SELECT runs.payload,d.state,d.attempts,d.cycle_attempts,d.next_attempt,d.external_id,d.error_code,d.updated_at
                                FROM runs LEFT JOIN ticket_deliveries d ON d.run_id=runs.id WHERE runs.id=?""", (run_id,)).fetchone()
        if not row:
            raise KeyError("Run not found")
        run = json.loads(row[0])
        if row[1] is not None:
            run["ticket_delivery"] = dict(zip(("state", "attempts", "cycle_attempts", "next_attempt", "external_id", "error_code", "updated_at"), row[1:]))
        return run

    def recent(self):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM runs ORDER BY created_at DESC LIMIT 30").fetchall()
        return [json.loads(row[0]) for row in rows]

    def page(self, limit=20, before=None, user_id=None):
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self.connect() as db:
            filters, params = [], []
            if user_id is not None:
                filters.append("id IN (SELECT run_id FROM run_owners WHERE user_id=?)")
                params.append(user_id)
            if before:
                if user_id is not None:
                    self.require_owner(before, user_id, db)
                cursor = db.execute("SELECT created_at,id FROM runs WHERE id=?", (before,)).fetchone()
                if not cursor:
                    raise KeyError("History cursor not found")
                filters.append("(created_at,id) < (?,?)")
                params.extend(cursor)
            where = "WHERE " + " AND ".join(filters) if filters else ""
            rows = db.execute(f"SELECT payload FROM runs {where} ORDER BY created_at DESC,id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        items = []
        for row in rows[:limit]:
            run = json.loads(row[0])
            items.append({key: run.get(key) for key in ("id", "message", "status", "created_at", "mode", "version")})
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}

    def require_owner(self, run_id, user_id, db=None):
        if db is None:
            with self.connect() as connection:
                return self.require_owner(run_id, user_id, connection)
        row = db.execute("SELECT 1 FROM run_owners WHERE run_id=? AND user_id=?", (run_id, user_id)).fetchone()
        if not row:
            raise KeyError("Run not found")

    def record_usage(self, db, user_id, mode):
        now = int(time.time())
        db.execute("DELETE FROM usage_windows WHERE started<=?", (now - 86400,))
        total, live = db.execute(
            "SELECT COUNT(*),COUNT(CASE WHEN mode='live' THEN 1 END) FROM usage_windows WHERE user_id=?",
            (user_id,),
        ).fetchone()
        global_total = db.execute("SELECT COUNT(*) FROM usage_windows").fetchone()[0]
        if global_total >= 500:
            raise QueueFull("Relay reached its daily investigation limit.")
        if total >= 25 or (mode == "live" and live >= 3):
            raise QueueFull("You reached your daily investigation limit.")
        if mode == "live":
            daily = db.execute("SELECT COUNT(*) FROM usage_windows WHERE mode='live'").fetchone()[0]
            if daily >= self.daily_live_limit:
                raise QueueFull("Relay reached its daily live investigation limit. Demo investigations are available.")
        db.execute("INSERT INTO usage_windows VALUES (?,?,?)", (user_id, mode, now))

    def list_policies(self, user_id, db=None):
        if db is None:
            with self.connect() as connection:
                return self.list_policies(user_id, connection)
        rows = db.execute(
            "SELECT id,title,text FROM workspace_policies WHERE user_id=? ORDER BY created_at,id", (user_id,)
        ).fetchall()
        return [dict(zip(("id", "title", "text"), row)) for row in rows]

    def add_policy(self, user_id, title, text):
        if not isinstance(title, str) or not isinstance(text, str):
            raise ValueError("Provide a policy title and text.")
        title, text = title.strip(), text.strip()
        if not 1 <= len(title) <= 120 or not 1 <= len(text) <= 3000:
            raise ValueError("Use a title of 1–120 characters and policy text of 1–3,000 characters.")
        if "\0" in title or "\0" in text:
            raise ValueError("Upload a UTF-8 text or Markdown policy.")
        try:
            title.encode("utf-8")
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Upload a UTF-8 text or Markdown policy.") from None
        identifier = "POL-" + uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM workspace_policies WHERE user_id=?", (user_id,)).fetchone()[0]
            if count >= 12:
                raise ValueError("Your workspace can store up to 12 policies. Remove one to add another.")
            db.execute("INSERT INTO workspace_policies VALUES (?,?,?,?,?)",
                       (identifier, user_id, title, text, datetime.now(timezone.utc).isoformat()))
        return {"id": identifier, "title": title, "text": text}

    def remove_policy(self, user_id, policy_id):
        if not isinstance(policy_id, str):
            raise KeyError("Policy not found")
        with self.connect() as db:
            if db.execute("DELETE FROM workspace_policies WHERE user_id=? AND id=?", (user_id, policy_id)).rowcount != 1:
                raise KeyError("Policy not found")

    def run_policies(self, run_id, db=None):
        if db is None:
            with self.connect() as connection:
                return self.run_policies(run_id, connection)
        row = db.execute("SELECT payload FROM run_policy_snapshots WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else []

    def enqueue(self, request, request_key, capacity=100, retry_of=None, user_id=None):
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
            raise ValueError("Provide a request_id of 1–128 characters")
        if user_id is not None:
            request_key = hashlib.sha256((user_id + ":" + request_key).encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps({**request, "retry_of": retry_of}, sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT run_id,fingerprint FROM jobs WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if existing[1] != fingerprint:
                    raise ValueError("request_id was already used for different input")
                return json.loads(db.execute("SELECT payload FROM runs WHERE id=?", (existing[0],)).fetchone()[0])
            if retry_of:
                if user_id is not None:
                    self.require_owner(retry_of, user_id, db)
                previous = db.execute("SELECT payload FROM runs WHERE id=?", (retry_of,)).fetchone()
                if not previous:
                    raise KeyError("Run not found")
                if json.loads(previous[0])["status"] not in {"failed", "interrupted"}:
                    raise ValueError("Only failed or interrupted investigations can be retried")
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= capacity:
                raise QueueFull("Investigation queue is full. Wait for a job to finish and retry.")
            now = datetime.now(timezone.utc).isoformat()
            run = {**request, "id": uuid.uuid4().hex, "created_at": now, "updated_at": now, "status": "queued", "version": 1,
                   "answer": None, "trace": [], "evidence": [], "plan": None, "usage": {"input_tokens": 0, "output_tokens": 0}, "duration_ms": 0}
            if retry_of:
                run["retry_of"] = retry_of
            policies = []
            if user_id is not None and request.get("use_policies"):
                policies = self.run_policies(retry_of, db) if retry_of else self.list_policies(user_id, db)
                run["policy_count"] = len(policies)
            db.execute("INSERT INTO runs VALUES (?,?,?)", (run["id"], now, json.dumps(run)))
            if policies:
                db.execute("INSERT INTO run_policy_snapshots VALUES (?,?)", (run["id"], json.dumps(policies)))
            if user_id is not None:
                db.execute("INSERT INTO run_owners VALUES (?,?)", (run["id"], user_id))
                self.record_usage(db, user_id, request["mode"])
            db.execute("INSERT INTO jobs VALUES (?,'queued',NULL,NULL,?,?)", (run["id"], request_key, fingerprint))
        return run

    @staticmethod
    def _update(db, run, changes):
        run.update(changes)
        run["version"] = run.get("version", 0) + 1
        run["updated_at"] = datetime.now(timezone.utc).isoformat()
        db.execute("UPDATE runs SET payload=? WHERE id=?", (json.dumps(run), run["id"]))
        return run

    def claim(self, owner, lease_seconds=90, provider="any", only_run=None, concurrency_limit=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute("SELECT run_id FROM jobs WHERE state='running' AND lease_until<?", (time.time(),)).fetchall()
            for (run_id,) in expired:
                run = json.loads(db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()[0])
                cancelled = bool(run.get("cancel_requested"))
                self._update(db, run, {"status": "cancelled" if cancelled else "interrupted", "error": None if cancelled else "Worker stopped before completion. Resume a compatible saved live conversation, or retry as a new investigation.", "resume_available": False if cancelled else run.get("resume_available")})
                db.execute("UPDATE jobs SET state=?,owner=NULL,lease_until=NULL WHERE run_id=?", ("cancelled" if cancelled else "interrupted", run_id))
                if cancelled:
                    db.execute("DELETE FROM agent_checkpoints WHERE run_id=?", (run_id,))
            if concurrency_limit is not None:
                active = db.execute("SELECT COUNT(*) FROM jobs WHERE state='running'").fetchone()[0]
                if active >= concurrency_limit:
                    return None
            restriction = "AND jobs.run_id=?" if only_run else ""
            params = (provider, provider, only_run) if only_run else (provider, provider)
            row = db.execute(f"""SELECT jobs.run_id,runs.payload FROM jobs JOIN runs ON runs.id=jobs.run_id
            WHERE jobs.state='queued' AND (?='any' OR json_extract(runs.payload,'$.mode')='demo'
            OR (json_extract(runs.payload,'$.mode')='live' AND json_extract(runs.payload,'$.provider')=?))
            {restriction} ORDER BY runs.created_at,runs.id LIMIT 1""", params).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='running',owner=?,lease_until=? WHERE run_id=?", (owner, time.time() + lease_seconds, row[0]))
            return self._update(db, json.loads(row[1]), {"status": "running"})

    def heartbeat(self, owner):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            db.execute("UPDATE jobs SET lease_until=? WHERE state='running' AND owner=? AND lease_until>=?", (now + 90, owner, now))

    def checkpoint(self, run_id, owner, changes, terminal=False, private_state=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT state,owner,lease_until FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            if not job or job[0] != "running" or job[1] != owner or job[2] < time.time():
                raise LeaseLost("Worker no longer owns this investigation")
            run = json.loads(db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()[0])
            if run.get("cancel_requested") and changes.get("status") != "cancelled":
                raise CancelRequested("Cancellation requested")
            self._update(db, run, changes)
            if private_state is not None:
                validate_state(private_state)
                encoded = json.dumps(private_state, allow_nan=False)
                db.execute("INSERT INTO agent_checkpoints VALUES (?,1,?,?) ON CONFLICT(run_id) DO UPDATE SET revision=agent_checkpoints.revision+1,checksum=excluded.checksum,state=excluded.state",
                           (run_id, digest(private_state), encoded))
            if terminal and run["status"] in {"awaiting_approval", "cancelled"}:
                db.execute("DELETE FROM agent_checkpoints WHERE run_id=?", (run_id,))
            if terminal:
                db.execute("UPDATE jobs SET state=?,owner=NULL,lease_until=NULL WHERE run_id=?", (run["status"], run_id))
            else:
                db.execute("UPDATE jobs SET lease_until=? WHERE run_id=?", (time.time() + 90, run_id))
        return run

    def cancel_run(self, run_id):
        """Queue cancellation is immediate; active work is fenced at its next boundary."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT r.payload,j.state FROM runs r JOIN jobs j ON j.run_id=r.id WHERE r.id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError("Run not found")
            run = json.loads(row[0])
            if run["status"] == "cancelled":
                return run
            if row[1] not in {"queued", "running", "interrupted"}:
                raise ValueError("Only queued, running or interrupted investigations can be cancelled")
            if row[1] == "running":
                if not run.get("cancel_requested"):
                    self._update(db, run, {"cancel_requested": True})
            else:
                self._update(db, run, {"status": "cancelled", "cancel_requested": True, "resume_available": False, "error": None})
                db.execute("UPDATE jobs SET state='cancelled',owner=NULL,lease_until=NULL WHERE run_id=?", (run_id,))
                db.execute("DELETE FROM agent_checkpoints WHERE run_id=?", (run_id,))
        return self.get(run_id)

    def cancellation_requested(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError("Run not found")
        return bool(json.loads(row[0]).get("cancel_requested"))

    def finish_cancel(self, run_id, owner):
        return self.checkpoint(run_id, owner, {"status": "cancelled", "cancel_requested": True,
                    "resume_available": False, "error": None}, terminal=True)

    def metrics(self):
        with self.connect() as db:
            states = dict(db.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state").fetchall())
            tickets = db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
            deliveries = dict(db.execute("SELECT state,COUNT(*) FROM ticket_deliveries GROUP BY state").fetchall())
        return {"jobs": states, "tickets": tickets, "ticket_deliveries": deliveries}

    def decide(self, run_id, decision, delivery_target=None):
        if decision not in {"approve", "reject"}:
            raise ValueError("Decision must be approve or reject")
        if delivery_target is not None:
            validate_destination(delivery_target)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError("Run not found")
            run = json.loads(row[0])
            if run["status"] not in {"awaiting_approval", "approved", "rejected"}:
                raise ValueError("This investigation has no completed proposal to review")
            if run["status"] != "awaiting_approval":
                expected = "approved" if decision == "approve" else "rejected"
                if run["status"] != expected:
                    raise ValueError("This run already has a different decision")
                return self.get(run_id)
            run["status"] = "approved" if decision == "approve" else "rejected"
            if decision == "approve":
                ticket_id = "TKT-" + uuid.uuid4().hex[:8].upper()
                db.execute("INSERT INTO tickets VALUES (?, ?, ?, ?)", (ticket_id, run_id, run["answer"]["action"], datetime.now(timezone.utc).isoformat()))
                run["ticket_id"] = ticket_id
                if delivery_target is not None:
                    payload = validate_payload({"ticket_id": ticket_id, "run_id": run_id,
                                                **{key: run["answer"][key] for key in ("action", "summary", "reason", "citations")}})
                    db.execute("INSERT INTO ticket_deliveries (ticket_id,run_id,destination,idempotency_key,payload,state,updated_at) VALUES (?,?,?,?,?,'pending',?)",
                               (ticket_id, run_id, delivery_target, "relay-" + run_id, json.dumps(payload), datetime.now(timezone.utc).isoformat()))
            run["decided_at"] = datetime.now(timezone.utc).isoformat()
            self._update(db, run, {})
            db.execute("UPDATE jobs SET state=? WHERE run_id=?", (run["status"], run_id))
        return self.get(run_id)

    def claim_delivery(self, owner, destination, lease_seconds=30):
        validate_destination(destination)
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            exhausted = db.execute("SELECT ticket_id,run_id FROM ticket_deliveries WHERE destination=? AND cycle_attempts>=5 AND (state='pending' OR (state='sending' AND lease_until<?))", (destination, now)).fetchall()
            for ticket_id, run_id in exhausted:
                db.execute("UPDATE ticket_deliveries SET state='failed',owner=NULL,lease_until=NULL,error_code='attempt_limit',next_attempt=0,updated_at=? WHERE ticket_id=?",
                           (datetime.now(timezone.utc).isoformat(), ticket_id))
                self._touch_delivery(db, run_id)
            row = db.execute("SELECT ticket_id,run_id,idempotency_key,payload FROM ticket_deliveries WHERE destination=? AND cycle_attempts<5 AND next_attempt<=? AND (state='pending' OR (state='sending' AND lease_until<?)) ORDER BY rowid LIMIT 1", (destination, now, now)).fetchone()
            if not row:
                return None
            db.execute("UPDATE ticket_deliveries SET state='sending',owner=?,lease_until=?,attempts=attempts+1,cycle_attempts=cycle_attempts+1,updated_at=? WHERE ticket_id=?",
                       (owner, now + lease_seconds, datetime.now(timezone.utc).isoformat(), row[0]))
            self._touch_delivery(db, row[1])
        return {"ticket_id": row[0], "run_id": row[1], "idempotency_key": row[2], "payload": json.loads(row[3])}

    @staticmethod
    def _touch_delivery(db, run_id):
        run = json.loads(db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()[0])
        Store._update(db, run, {})

    def finish_delivery(self, ticket_id, owner, external_id=None, error_code=None, retryable=False, retry_after=0):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT run_id,state,owner,lease_until,cycle_attempts FROM ticket_deliveries WHERE ticket_id=?", (ticket_id,)).fetchone()
            if not row or row[1] != "sending" or row[2] != owner or row[3] < time.time():
                raise LeaseLost("Ticket delivery lease is no longer owned")
            state = "delivered" if external_id else "pending" if retryable and row[4] < 5 else "failed"
            delay = max(min(60, 2 ** min(row[4] - 1, 6)), retry_after)
            db.execute("UPDATE ticket_deliveries SET state=?,owner=NULL,lease_until=NULL,external_id=?,error_code=?,next_attempt=?,updated_at=? WHERE ticket_id=?",
                       (state, external_id, error_code, time.time() + delay if state == "pending" else 0, datetime.now(timezone.utc).isoformat(), ticket_id))
            self._touch_delivery(db, row[0])

    def retry_delivery(self, run_id, destination):
        validate_destination(destination)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,destination FROM ticket_deliveries WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("No sandbox ticket delivery exists for this run")
            if row[1] != destination:
                raise ValueError("Restart with this ticket's original sandbox destination")
            if row[0] not in {"failed", "pending"}:
                raise ValueError("Only failed ticket delivery can be retried")
            if row[0] == "failed":
                db.execute("UPDATE ticket_deliveries SET state='pending',cycle_attempts=0,next_attempt=0,error_code=NULL,updated_at=? WHERE run_id=?",
                           (datetime.now(timezone.utc).isoformat(), run_id))
                self._touch_delivery(db, run_id)
        return self.get(run_id)

    def load_agent_checkpoint(self, run_id, owner=None):
        with self.connect() as db:
            row = db.execute("SELECT c.state,c.checksum,j.state,j.owner,j.lease_until FROM jobs j LEFT JOIN agent_checkpoints c ON c.run_id=j.run_id WHERE j.run_id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError("Run not found")
        if owner is not None:
            if row[2] != "running" or row[3] != owner or row[4] < time.time():
                raise LeaseLost("Worker no longer owns this conversation")
        elif row[2] not in {"failed", "interrupted"}:
            raise ValueError("Only failed or interrupted live investigations can be resumed")
        if row[0] is None:
            return None
        try:
            state = json.loads(row[0])
            validate_state(state)
            if digest(state) != row[1]:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("Saved conversation is invalid. Start a new investigation.") from None
        return state

    def resume_receipt(self, run_id, request_key):
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
            raise ValueError("Provide a request_id of 1–128 characters")
        with self.connect() as db:
            row = db.execute("SELECT run_id FROM resume_requests WHERE request_key=?", (request_key,)).fetchone()
        if row and row[0] != run_id:
            raise ValueError("Resume request_id was already used for another investigation")
        return self.get(run_id) if row else None

    def resume_run(self, run_id, request_key, capacity=100, user_id=None):
        # Configuration is checked by JobRunner before this atomic state transition.
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
            raise ValueError("Provide a request_id of 1–128 characters")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            receipt = db.execute("SELECT run_id FROM resume_requests WHERE request_key=?", (request_key,)).fetchone()
            if receipt:
                if receipt[0] != run_id:
                    raise ValueError("Resume request_id was already used for another investigation")
                return self.get(run_id)
            row = db.execute("SELECT r.payload,j.state,c.state,c.checksum FROM runs r JOIN jobs j ON j.run_id=r.id LEFT JOIN agent_checkpoints c ON c.run_id=r.id WHERE r.id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError("Run not found")
            run = json.loads(row[0])
            if run["mode"] != "live" or run["status"] not in {"failed", "interrupted"} or row[1] != run["status"] or row[2] is None:
                raise ValueError("No resumable failed or interrupted live conversation exists")
            state = validate_state(json.loads(row[2]))
            if digest(state) != row[3] or not resumable(state):
                raise ValueError("Saved conversation is invalid or exhausted its budget. Start a new investigation.")
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= capacity:
                raise QueueFull("Investigation queue is full. Wait for a job to finish and retry.")
            if user_id is not None:
                self.require_owner(run_id, user_id, db)
                self.record_usage(db, user_id, "live")
            db.execute("INSERT INTO resume_requests VALUES (?,?)", (request_key, run_id))
            db.execute("UPDATE jobs SET state='queued',owner=NULL,lease_until=NULL WHERE run_id=?", (run_id,))
            self._update(db, run, {"status": "queued", "error": None, "resume_count": run.get("resume_count", 0) + 1})
        return self.get(run_id)
