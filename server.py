"""Local portfolio server. Run: python3 server.py."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from supportops.agent import investigate, provider_config
from supportops.store import Store

ROOT = Path(__file__).parent
STORE = None
SLOTS = threading.BoundedSemaphore(2)


class Handler(BaseHTTPRequestHandler):
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
        path = urlparse(self.path).path
        if path == "/api/config":
            return self.send(200, {**provider_config(), "accounts": ["acct_demo", "acct_basic", "acct_locked"]})
        if path == "/api/runs":
            return self.send(200, STORE.recent())
        if path.startswith("/api/runs/"):
            try:
                return self.send(200, STORE.get(path.rsplit("/", 1)[-1]))
            except KeyError:
                return self.send(404, {"error": "Run not found"})
        assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8")}
        if path in assets:
            file, content_type = assets[path]
            return self.send(200, (ROOT / "web" / file).read_bytes(), content_type)
        return self.send(404, {"error": "Not found"})

    def do_POST(self):
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
                if not SLOTS.acquire(blocking=False):
                    return self.send(429, {"error": "Two investigations are already running. Try again shortly."})
                try:
                    run = investigate(body.get("message"), body.get("account_id", "acct_demo"), body.get("mode", "demo"))
                    return self.send(201, STORE.save(run))
                finally:
                    SLOTS.release()
            if path.startswith("/api/runs/") and path.endswith("/decision"):
                run_id = path.split("/")[-2]
                return self.send(200, STORE.decide(run_id, body.get("decision")))
            return self.send(404, {"error": "Not found"})
        except (ValueError, TypeError) as exc:
            return self.send(400, {"error": str(exc)})
        except KeyError:
            return self.send(404, {"error": "Run not found"})
        except RuntimeError as exc:
            return self.send(502, {"error": str(exc)})
        except Exception:
            return self.send(500, {"error": "Investigation failed. No review decision was committed."})


if __name__ == "__main__":
    STORE = Store()
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"SupportOps is running at http://127.0.0.1:{port}", flush=True)
    server.serve_forever()
