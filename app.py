"""WSGI entry point for the hosted Relay workspace."""
from io import BytesIO
import json
import os
import threading
from types import SimpleNamespace

from flask import Flask, Response, request
from werkzeug.exceptions import HTTPException
from server import Handler
from supportops.auth import Accounts
from supportops.google_login import GoogleLogin
from supportops.jobs import JobRunner
from supportops.store import Store

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 20_000
_runtime = None
_runtime_lock = threading.Lock()


def runtime():
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            project = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")
            origin = os.environ.get("RELAY_PUBLIC_ORIGIN") or (f"https://{project}" if project else "")
            if not origin or (not origin.startswith("https://") and not origin.startswith("http://127.0.0.1:")):
                raise RuntimeError("Missing hosted origin")
            url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
            if not url:
                raise RuntimeError("Missing hosted database")
            store = Store(database_url=url, daily_live_limit=int(os.environ.get("RELAY_DAILY_LIVE_LIMIT", "8")))
            _runtime = SimpleNamespace(
                public_origin=origin, server_port=443, accounts=Accounts(store), store=store,
                google_login=GoogleLogin.configured(origin),
                jobs=JobRunner(store, workers=2, capacity=30), deliveries=None, request_worker=True,
            )
    return _runtime


class WSGIHandler(Handler):
    def __init__(self, server):
        self.server = server
        self.headers = request.headers
        self.path = request.full_path
        self.rfile = BytesIO(request.get_data() if request.method == "POST" else b"")
        self.response = None

    def send(self, status, payload, content_type="application/json", cookies=(), location=None):
        encoded = json.dumps(payload) if content_type == "application/json" else payload
        self.response = Response(encoded, status=status, content_type=content_type, headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        })
        for cookie in cookies:
            self.response.headers.add("Set-Cookie", cookie)
        if location:
            self.response.headers["Location"] = location


@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def relay(path):
    try:
        handler = WSGIHandler(runtime())
        if request.method == "POST":
            handler.do_POST()
        else:
            handler.do_GET()
        return handler.response
    except HTTPException:
        raise
    except Exception:
        return {"error": "The workspace is temporarily unavailable."}, 503


@app.errorhandler(413)
def body_too_large(error):
    return {"error": "Request must be between 1 and 20000 bytes"}, 413
