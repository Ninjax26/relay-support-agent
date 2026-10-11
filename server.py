"""Local support operations server. Run: python3 server.py."""
import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from supportops.agent import provider_config
from supportops.store import Store, QueueFull
from supportops.jobs import JobRunner

ROOT = Path(__file__).parent



class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def valid_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"localhost:{port}", f"127.0.0.1:{port}"}

    def send(self, status, payload, content_type="application/json"):
        body = json.dumps(payload).encode() if content_type == "application/json" else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.valid_host():
            return self.send(400, {"error": "Use the localhost or 127.0.0.1 address shown by Relay"})
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            return self.send(200, {"status": "ok", "workers": self.server.jobs.workers, "queue_capacity": self.server.jobs.capacity, **self.server.store.metrics()})
        if path == "/api/config":
            return self.send(200, {**provider_config(), "accounts": ["acct_demo", "acct_basic", "acct_locked"]})
        if path == "/api/runs":
            try:
                params = parse_qs(parsed.query)
                return self.send(200, self.server.store.page(int(params.get("limit", ["20"])[0]), params.get("before", [None])[0]))
            except ValueError as exc:
                return self.send(400, {"error": str(exc)})
            except KeyError:
                return self.send(404, {"error": "History cursor not found"})
        if path.startswith("/api/runs/"):
            try:
                return self.send(200, self.server.store.get(path.rsplit("/", 1)[-1]))
            except KeyError:
                return self.send(404, {"error": "Run not found"})
        assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8")}
        if path in assets:
            file, content_type = assets[path]
            return self.send(200, (ROOT / "web" / file).read_bytes(), content_type)
        return self.send(404, {"error": "Not found"})

    def do_POST(self):
        if not self.valid_host():
            return self.send(400, {"error": "Use the localhost or 127.0.0.1 address shown by Relay"})
        # Same-origin JSON requests only; bind locally, never expose this demo as a public API.
        origin = self.headers.get("Origin")
        port = self.server.server_port
        if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            return self.send(403, {"error": "Cross-origin requests are not allowed"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(415, {"error": "Use application/json"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 20000:
                raise ValueError("Request must be between 1 and 20000 bytes")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Request must be a JSON object")
            path = urlparse(self.path).path
            if path == "/api/runs":
                request = {"message": body.get("message"), "account_id": body.get("account_id", "acct_demo"), "mode": body.get("mode", "demo")}
                run = self.server.jobs.submit(request, body.get("request_id", uuid.uuid4().hex))
                return self.send(202, run)
            if path.startswith("/api/runs/") and path.endswith("/retry"):
                run_id = path.split("/")[-2]
                previous = self.server.store.get(run_id)
                request = {key: previous[key] for key in ("message", "account_id", "mode")}
                return self.send(202, self.server.jobs.submit(request, body.get("request_id", uuid.uuid4().hex), retry_of=run_id))
            if path.startswith("/api/runs/") and path.endswith("/decision"):
                run_id = path.split("/")[-2]
                return self.send(200, self.server.store.decide(run_id, body.get("decision")))
            return self.send(404, {"error": "Not found"})
        except (ValueError, TypeError) as exc:
            return self.send(400, {"error": str(exc)})
        except KeyError:
            return self.send(404, {"error": "Run not found"})
        except QueueFull as exc:
            return self.send(429, {"error": str(exc)})
        except RuntimeError as exc:
            return self.send(502, {"error": str(exc)})
        except Exception:
            return self.send(500, {"error": "Investigation failed. No review decision was committed."})


def create_server(port=8000, store=None, workers=None, capacity=None, start_workers=True):
    store = store or Store(os.environ.get("RELAY_DB_PATH", str(ROOT / "data/supportops.sqlite3")))
    jobs = JobRunner(store, workers if workers is not None else int(os.environ.get("RELAY_WORKERS", "2")),
                     capacity if capacity is not None else int(os.environ.get("RELAY_QUEUE_CAPACITY", "100")))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.store, server.jobs = store, jobs
    if start_workers:
        jobs.start()
    return server


if __name__ == "__main__":
    server = create_server(int(os.environ.get("PORT", "8000")))
    print(f"Relay is running at http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.jobs.close()
        server.server_close()
