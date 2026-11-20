"""Relay workspace HTTP server. Run: python3 server.py."""
import json
import os
import re
import uuid
import time
import threading
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from supportops.auth import Accounts, Throttled
from supportops.google_login import CALLBACK_PATH, GoogleLogin, GoogleLoginError
from supportops.agent import provider_config
from supportops.retrieval import configured_mode
from supportops.store import Store, QueueFull
from supportops.jobs import JobRunner
from supportops.ticketing import TicketConnector
from supportops.deliveries import DeliveryRunner

ROOT = Path(__file__).parent
RUN_ROUTE = re.compile(r"/api/runs/([a-zA-Z0-9_-]+)(?:/(events|cancel|resume|ticket/retry|retry|decision|process))?")



class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def valid_host(self):
        if self.server.public_origin:
            return self.headers.get("Host") == urlparse(self.server.public_origin).netloc
        port = self.server.server_port
        return self.headers.get("Host") in {f"localhost:{port}", f"127.0.0.1:{port}"}

    def read_cookie(self, name):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            return cookies[name].value if name in cookies else None
        except CookieError:
            return None

    def session_token(self):
        return self.read_cookie("relay_session")

    def session_user(self):
        return self.server.accounts.identify(self.session_token()) if self.server.accounts else None

    def require_run(self, run_id, user):
        if user:
            self.server.store.require_owner(run_id, user["id"])

    def cookie(self, name, token, max_age, same_site="Strict"):
        secure = "; Secure" if self.server.public_origin.startswith("https://") else ""
        return f"{name}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite={same_site}{secure}"

    def google_cookie_name(self):
        return "__Host-relay_google_state" if self.server.public_origin.startswith("https://") else "relay_google_state"

    def google_callback(self, query):
        accounts = self.server.accounts
        cookie_name = self.google_cookie_name()
        expired = self.cookie(cookie_name, "", 0, "Lax")
        state, session = None, ()
        destination = "/?signin=google-failed"
        try:
            params = parse_qs(query, max_num_fields=20)
            states = params.get("state", [])
            state = accounts.consume_google_login(states[0], self.read_cookie(cookie_name)) if len(states) == 1 else None
            codes = params.get("code", [])
            if state and params.get("error") == ["access_denied"]:
                destination = "/?signin=google-cancelled"
            elif state and not params.get("error") and len(codes) == 1 and 0 < len(codes[0]) <= 4096:
                subject, name = self.server.google_login.exchange(codes[0], *state)
                token = accounts.sign_in_with_google(subject, name)
                accounts.logout(self.session_token())
                session = (self.cookie("relay_session", token, accounts.lifetime, "Lax"),)
                destination = "/"
        except (GoogleLoginError, ValueError):
            pass
        # An unrelated cross-site visit must not erase a legitimate pending sign-in.
        cookies = (expired, *session) if state else ()
        return self.send(303, b"", "text/plain; charset=utf-8", cookies=cookies, location=destination)

    def log_request(self, code="-", size="-"):
        if self.path.partition("?")[0] == CALLBACK_PATH:
            self.log_message("%s %s %s %s", self.command, CALLBACK_PATH, code, size)
        else:
            super().log_request(code, size)

    def send(self, status, payload, content_type="application/json", cookies=(), location=None):
        body = json.dumps(payload).encode() if content_type == "application/json" else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health" and self.server.accounts:
            try:
                with self.server.store.connect() as db:
                    db.execute("SELECT 1 FROM users LIMIT 1").fetchone()
            except Exception:
                return self.send(503, {"status": "unavailable"})
            return self.send(200, {"status": "ok", "revision": os.environ.get("RENDER_GIT_COMMIT")})
        if not self.valid_host():
            return self.send(400, {"error": "Unrecognized Relay host"})
        if path == CALLBACK_PATH and self.server.google_login:
            return self.google_callback(parsed.query)
        assets = {"/google-sign-in.png": ("google-sign-in.png", "image/png"), "/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8")}
        if path in assets:
            file, content_type = assets[path]
            return self.send(200, (ROOT / "web" / file).read_bytes(), content_type)
        if path == "/api/health":
            return self.send(200, {"status": "ok", "workers": self.server.jobs.workers, "queue_capacity": self.server.jobs.capacity, **self.server.store.metrics()})
        user = self.session_user()
        if path == "/api/session":
            return self.send(200, {"required": bool(self.server.accounts), "user": user,
                                   "google_login": bool(self.server.google_login)})
        if self.server.accounts and not user:
            return self.send(401, {"error": "Sign in to open your Relay workspace."})
        if path == "/api/policies" and user:
            return self.send(200, {"items": self.server.store.list_policies(user["id"])})
        if path == "/api/config":
            return self.send(200, {**provider_config(), "retrieval_mode": configured_mode(), "ticket_connector": "sandbox" if self.server.deliveries else "local_only", "request_worker": getattr(self.server, "request_worker", False), "accounts": ["acct_demo", "acct_basic", "acct_locked"]})
        if path == "/api/runs":
            try:
                params = parse_qs(parsed.query)
                return self.send(200, self.server.store.page(int(params.get("limit", ["20"])[0]), params.get("before", [None])[0], user["id"] if user else None))
            except ValueError as exc:
                return self.send(400, {"error": str(exc)})
            except KeyError:
                return self.send(404, {"error": "History cursor not found"})
        route = RUN_ROUTE.fullmatch(path)
        if route and route[2] in {None, "events"}:
            run_id = route[1]
            try:
                self.require_run(run_id, user)
                if route[2] == "events":
                    if getattr(self.server, "request_worker", False):
                        return self.send(404, {"error": "Use the run status endpoint."})
                    return self.stream_run(run_id, user)
                return self.send(200, self.server.store.get(run_id))
            except KeyError:
                return self.send(404, {"error": "Run not found"})
        return self.send(404, {"error": "Not found"})

    def stream_run(self, run_id, user=None):
        run = self.server.store.get(run_id)
        try:
            cursor = int(self.headers.get("Last-Event-ID", "-1"))
        except ValueError:
            cursor = -1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and not self.server.stream_stop.is_set():
            version = run.get("version", 0)
            if version > cursor:
                if user and not self.session_user():
                    return
                payload = f'id: {version}\ndata: {json.dumps(run)}\n\n'.encode()
                try:
                    self.wfile.write(payload)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    return
                cursor = version
            if run["status"] not in {"queued", "running"} and run.get("ticket_delivery", {}).get("state") not in {"pending", "sending"}:
                return
            if self.server.stream_stop.wait(0.25):
                return
            run = self.server.store.get(run_id)

    def do_POST(self):
        if not self.valid_host():
            return self.send(400, {"error": "Unrecognized Relay host"})
        origin = self.headers.get("Origin")
        port = self.server.server_port
        if self.server.public_origin:
            origin_allowed = origin == self.server.public_origin
        else:
            origin_allowed = not origin or origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if not origin_allowed:
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
            accounts = self.server.accounts
            if self.server.google_login and path == "/api/session/google/start":
                state, nonce, verifier = accounts.start_google_login()
                cookie = self.cookie(self.google_cookie_name(), state, accounts.google_state_lifetime, "Lax")
                return self.send(200, {"url": self.server.google_login.authorization_url(state, nonce, verifier)},
                                 cookies=(cookie,))
            if accounts and path in {"/api/session/register", "/api/session/login"}:
                token = accounts.authenticate(body.get("username"), body.get("password"), register=path.endswith("/register"))
                if token is None:
                    return self.send(401, {"error": "Incorrect username or password."})
                accounts.logout(self.session_token())
                return self.send(200, {"ok": True}, cookies=(self.cookie("relay_session", token, accounts.lifetime),))
            if accounts and path == "/api/session/logout":
                accounts.logout(self.session_token())
                return self.send(200, {"ok": True}, cookies=(self.cookie("relay_session", "", 0),))
            user = self.session_user()
            if accounts and not user:
                return self.send(401, {"error": "Sign in to open your Relay workspace."})
            user_id = user["id"] if user else None
            if path == "/api/policies" and user:
                return self.send(201, self.server.store.add_policy(user_id, body.get("title"), body.get("text")))
            if path == "/api/policies/remove" and user:
                try:
                    self.server.store.remove_policy(user_id, body.get("id"))
                except KeyError:
                    return self.send(404, {"error": "Policy not found"})
                return self.send(200, {"ok": True})
            if path == "/api/runs":
                request = {"message": body.get("message"), "account_id": body.get("account_id", "acct_demo"), "mode": body.get("mode", "demo")}
                use_policies = body.get("use_policies", False)
                if not isinstance(use_policies, bool):
                    raise ValueError("use_policies must be true or false")
                if user and use_policies:
                    request["use_policies"] = True
                run = self.server.jobs.submit(request, body.get("request_id", uuid.uuid4().hex), user_id=user_id)
                return self.send(202, run)
            route = RUN_ROUTE.fullmatch(path)
            if not route:
                return self.send(404, {"error": "Not found"})
            run_id, action = route.groups()
            self.require_run(run_id, user)
            if action == "process" and getattr(self.server, "request_worker", False):
                self.server.jobs.run_one(only_run=run_id, concurrency_limit=self.server.jobs.workers)
                return self.send(200, self.server.store.get(run_id))
            if action == "cancel":
                return self.send(200, self.server.jobs.cancel(run_id))
            if action == "resume":
                return self.send(202, self.server.jobs.resume(run_id, body.get("request_id", uuid.uuid4().hex), user_id=user_id))
            if action == "ticket/retry":
                if not self.server.deliveries:
                    raise ValueError("Start Relay with the ticket sandbox to retry delivery")
                return self.send(200, self.server.store.retry_delivery(run_id, self.server.deliveries.connector.destination))
            if action == "retry":
                previous = self.server.store.get(run_id)
                request = {key: previous[key] for key in ("message", "account_id", "mode")}
                if previous.get("use_policies"):
                    request["use_policies"] = True
                return self.send(202, self.server.jobs.submit(request, body.get("request_id", uuid.uuid4().hex), retry_of=run_id, user_id=user_id))
            if action == "decision":
                return self.send(200, self.server.store.decide(run_id, body.get("decision"), delivery_target=self.server.deliveries.connector.destination if self.server.deliveries else None))
            return self.send(404, {"error": "Not found"})
        except (ValueError, TypeError) as exc:
            return self.send(400, {"error": str(exc)})
        except KeyError:
            return self.send(404, {"error": "Run not found"})
        except (QueueFull, Throttled) as exc:
            return self.send(429, {"error": str(exc)})
        except RuntimeError as exc:
            return self.send(502, {"error": str(exc)})
        except Exception:
            return self.send(500, {"error": "Investigation failed. No review decision was committed."})


class RelayServer(ThreadingHTTPServer):
    def process_request(self, request, address):
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.connections.release()

    def server_close(self):
        if getattr(self, "stream_stop", None):
            self.stream_stop.set()
        if getattr(self, "deliveries", None):
            self.deliveries.close()
        if getattr(self, "jobs", None):
            self.jobs.close()
        super().server_close()


def create_server(port=8000, store=None, workers=None, capacity=None, start_workers=True, ticket_connector=None,
                  public_origin=None, bind="127.0.0.1"):
    if public_origin:
        parsed = urlparse(public_origin)
        origin_only = public_origin == f"{parsed.scheme}://{parsed.netloc}"
        secure = parsed.scheme == "https" or (parsed.scheme == "http" and bind == "127.0.0.1" and parsed.hostname == "127.0.0.1")
        if not origin_only or not secure or not parsed.hostname or parsed.username:
            raise ValueError("Configure the exact public HTTPS origin without a trailing slash.")
    elif bind != "127.0.0.1":
        raise ValueError("Public connections require authenticated hosting mode.")
    ticket_connector = ticket_connector or TicketConnector.from_environment()
    store = store or Store(os.environ.get("RELAY_DB_PATH", str(ROOT / "data/supportops.sqlite3")),
                           daily_live_limit=int(os.environ.get("RELAY_DAILY_LIVE_LIMIT", "8")),
                           database_url=os.environ.get("DATABASE_URL") if public_origin else None)
    accounts = Accounts(store) if public_origin else None
    jobs = JobRunner(store, workers if workers is not None else int(os.environ.get("RELAY_WORKERS", "2")),
                     capacity if capacity is not None else int(os.environ.get("RELAY_QUEUE_CAPACITY", "100")))
    server = RelayServer((bind, port), Handler)
    server.connections = threading.BoundedSemaphore(48)
    server.accounts, server.public_origin = accounts, public_origin
    server.google_login = GoogleLogin.configured(public_origin) if accounts else None
    server.daemon_threads = True
    server.stream_stop = threading.Event()
    server.store, server.jobs = store, jobs
    server.deliveries = DeliveryRunner(store, ticket_connector) if ticket_connector else None
    if start_workers:
        jobs.start()
        if server.deliveries:
            server.deliveries.start()
    return server


if __name__ == "__main__":
    public_origin = os.environ.get("RELAY_PUBLIC_ORIGIN") or os.environ.get("RENDER_EXTERNAL_URL")
    if public_origin and not (os.environ.get("RELAY_DB_PATH") or os.environ.get("DATABASE_URL")):
        raise SystemExit("Hosted Relay requires persistent storage.")
    server = create_server(int(os.environ.get("PORT", "8000")), public_origin=public_origin,
                           bind=os.environ.get("RELAY_BIND", "0.0.0.0" if public_origin else "127.0.0.1"))
    print(f"Relay is running at {public_origin or f'http://127.0.0.1:{server.server_port}'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
