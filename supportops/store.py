"""SQLite run checkpoints and transactional, idempotent review decisions."""
import json
import sqlite3
import uuid
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data/supportops.sqlite3"


class QueueFull(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class Store:
    def __init__(self, path=DEFAULT_DB):
        self.path = str(path)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, run_id TEXT UNIQUE NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (run_id TEXT PRIMARY KEY, state TEXT NOT NULL, owner TEXT, lease_until REAL,
            request_key TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, lease_until);
            CREATE INDEX IF NOT EXISTS runs_recent ON runs(created_at DESC, id DESC);""")

    @contextmanager
    def connect(self):
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
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError("Run not found")
        return json.loads(row[0])

    def recent(self):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM runs ORDER BY created_at DESC LIMIT 30").fetchall()
        return [json.loads(row[0]) for row in rows]

    def page(self, limit=20, before=None):
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self.connect() as db:
            where, params = "", []
            if before:
                cursor = db.execute("SELECT created_at,id FROM runs WHERE id=?", (before,)).fetchone()
                if not cursor:
                    raise KeyError("History cursor not found")
                where = "WHERE (created_at,id) < (?,?)"
                params = list(cursor)
            rows = db.execute(f"SELECT payload FROM runs {where} ORDER BY created_at DESC,id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        items = []
        for row in rows[:limit]:
            run = json.loads(row[0])
            items.append({key: run.get(key) for key in ("id", "message", "status", "created_at", "mode", "version")})
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}

    def enqueue(self, request, request_key, capacity=100, retry_of=None):
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
            raise ValueError("Provide a request_id of 1–128 characters")
        fingerprint = hashlib.sha256(json.dumps({**request, "retry_of": retry_of}, sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT run_id,fingerprint FROM jobs WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if existing[1] != fingerprint:
                    raise ValueError("request_id was already used for different input")
                return json.loads(db.execute("SELECT payload FROM runs WHERE id=?", (existing[0],)).fetchone()[0])
            if retry_of:
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
            db.execute("INSERT INTO runs VALUES (?,?,?)", (run["id"], now, json.dumps(run)))
            db.execute("INSERT INTO jobs VALUES (?,'queued',NULL,NULL,?,?)", (run["id"], request_key, fingerprint))
        return run

    @staticmethod
    def _update(db, run, changes):
        run.update(changes)
        run["version"] = run.get("version", 0) + 1
        run["updated_at"] = datetime.now(timezone.utc).isoformat()
        db.execute("UPDATE runs SET payload=? WHERE id=?", (json.dumps(run), run["id"]))
        return run

    def claim(self, owner, lease_seconds=90):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute("SELECT run_id FROM jobs WHERE state='running' AND lease_until<?", (time.time(),)).fetchall()
            for (run_id,) in expired:
                run = json.loads(db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()[0])
                self._update(db, run, {"status": "interrupted", "error": "Worker stopped before completion. Saved steps remain available. Retry starts a new investigation."})
                db.execute("UPDATE jobs SET state='interrupted',owner=NULL WHERE run_id=?", (run_id,))
            row = db.execute("SELECT jobs.run_id,runs.payload FROM jobs JOIN runs ON runs.id=jobs.run_id WHERE jobs.state='queued' ORDER BY runs.created_at,runs.id LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='running',owner=?,lease_until=? WHERE run_id=?", (owner, time.time() + lease_seconds, row[0]))
            return self._update(db, json.loads(row[1]), {"status": "running"})

    def heartbeat(self, owner):
        with self.connect() as db:
            now = time.time()
            db.execute("UPDATE jobs SET lease_until=? WHERE state='running' AND owner=? AND lease_until>=?", (now + 90, owner, now))

    def checkpoint(self, run_id, owner, changes, terminal=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT state,owner,lease_until FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            if not job or job[0] != "running" or job[1] != owner or job[2] < time.time():
                raise LeaseLost("Worker no longer owns this investigation")
            run = json.loads(db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()[0])
            self._update(db, run, changes)
            if terminal:
                db.execute("UPDATE jobs SET state=?,owner=NULL,lease_until=NULL WHERE run_id=?", (run["status"], run_id))
            else:
                db.execute("UPDATE jobs SET lease_until=? WHERE run_id=?", (time.time() + 90, run_id))
        return run

    def metrics(self):
        with self.connect() as db:
            states = dict(db.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state").fetchall())
            tickets = db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        return {"jobs": states, "tickets": tickets}

    def decide(self, run_id, decision):
        if decision not in {"approve", "reject"}:
            raise ValueError("Decision must be approve or reject")
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
                return run
            run["status"] = "approved" if decision == "approve" else "rejected"
            if decision == "approve":
                ticket_id = "TKT-" + uuid.uuid4().hex[:8].upper()
                db.execute("INSERT INTO tickets VALUES (?, ?, ?, ?)", (ticket_id, run_id, run["answer"]["action"], datetime.now(timezone.utc).isoformat()))
                run["ticket_id"] = ticket_id
            run["decided_at"] = datetime.now(timezone.utc).isoformat()
            self._update(db, run, {})
            db.execute("UPDATE jobs SET state=? WHERE run_id=?", (run["status"], run_id))
        return run
