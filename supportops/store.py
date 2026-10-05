"""SQLite run checkpoints and transactional, idempotent review decisions."""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data/supportops.sqlite3"


class Store:
    def __init__(self, path=DEFAULT_DB):
        self.path = str(path)
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, run_id TEXT UNIQUE NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL);""")

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

    def decide(self, run_id, decision):
        if decision not in {"approve", "reject"}:
            raise ValueError("Decision must be approve or reject")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError("Run not found")
            run = json.loads(row[0])
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
            db.execute("UPDATE runs SET payload=? WHERE id=?", (json.dumps(run), run_id))
        return run
