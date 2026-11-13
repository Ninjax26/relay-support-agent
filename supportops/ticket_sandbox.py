"""Independent local test ticket service with persistent idempotency records."""
import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .ticketing import validate_payload, MAX_TICKET_BYTES


class SandboxStore:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS sandbox_tickets (id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, key, payload):
        validate_payload(payload)
        if key != "relay-" + payload["run_id"]:
            raise ValueError("Invalid idempotency key")
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT id,fingerprint FROM sandbox_tickets WHERE idempotency_key=?", (key,)).fetchone()
            if previous:
                if previous[1] != fingerprint:
                    raise FileExistsError("Idempotency key already used for another payload")
                return previous[0], False
            identifier = "SBX-" + uuid.uuid4().hex[:12].upper()
            db.execute("INSERT INTO sandbox_tickets VALUES (?,?,?,?)", (identifier, key, fingerprint, json.dumps(payload)))
            return identifier, True

    def count(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM sandbox_tickets").fetchone()[0]


class TicketHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass  # Do not log request paths, credentials or ticket content.

    def send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def allowed(self):
        port = self.server.server_port
        if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"} or self.headers.get("Origin"):
            self.send(403, {"error": "Request not allowed"})
            return False
        supplied = self.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + self.server.token).encode()):
            self.send(401, {"error": "Authorization required"})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        if self.path != "/health":
            return self.send(404, {"error": "Not found"})
        self.send(200, {"status": "ok", "tickets": self.server.store.count()})

    def do_POST(self):
        if not self.allowed():
            return
        if self.path != "/tickets":
            return self.send(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Content-Type") != "application/json" or not 0 < length <= MAX_TICKET_BYTES:
                raise ValueError("Invalid request size or type")
            payload = json.loads(self.rfile.read(length))
            identifier, created = self.server.store.create(self.headers.get("Idempotency-Key"), payload)
            self.send(201 if created else 200, {"ticket_id": identifier, "status": "open"})
        except FileExistsError:
            self.send(409, {"error": "Idempotency conflict"})
        except (ValueError, TypeError, KeyError):
            self.send(400, {"error": "Invalid ticket request"})
        except Exception:
            self.send(500, {"error": "Ticket service unavailable"})


def create_ticket_server(store, token, port=8014):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,200}", token):
        raise ValueError("A sandbox credential is required")
    server = ThreadingHTTPServer(("127.0.0.1", port), TicketHandler)
    server.daemon_threads = True
    server.store, server.token = store, token
    return server
