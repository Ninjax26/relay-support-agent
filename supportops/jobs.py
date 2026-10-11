"""Bounded background workers over the persistent SQLite queue."""
import threading
import uuid
from .agent import investigate, validate_request, provider_config
from .store import LeaseLost


class JobRunner:
    def __init__(self, store, workers=2, capacity=100, agent=investigate):
        if not 1 <= workers <= 16 or not 1 <= capacity <= 5000:
            raise ValueError("Workers must be 1–16 and queue capacity 1–5000")
        self.store, self.workers, self.capacity, self.agent = store, workers, capacity, agent
        self.owner = uuid.uuid4().hex
        self.stop = threading.Event()
        self.threads = []

    def submit(self, request, request_key, retry_of=None):
        validate_request(request.get("message"), request.get("account_id"), request.get("mode"))
        if request["mode"] == "live":
            config = provider_config()
            if not config["live_available"]:
                raise ValueError("Live mode requires a configured provider key. Run python3 run_live.py locally.")
            request = {**request, "provider": config["provider"]}
        return self.store.enqueue(request, request_key, self.capacity, retry_of)

    def start(self):
        if self.threads:
            return
        for index in range(self.workers):
            thread = threading.Thread(target=self._worker, name=f"relay-worker-{index}", daemon=True)
            thread.start()
            self.threads.append(thread)
        heartbeat = threading.Thread(target=self._heartbeat, name="relay-heartbeat", daemon=True)
        heartbeat.start()
        self.threads.append(heartbeat)

    def _heartbeat(self):
        while not self.stop.wait(10):
            try:
                self.store.heartbeat(self.owner)
            except Exception:
                # A prolonged failure expires the lease and fences the worker.
                continue

    def run_one(self):
        config = provider_config()
        run = self.store.claim(self.owner, provider=config["provider"] if config["live_available"] else None)
        if not run:
            return False
        try:
            result = self.agent(run["message"], run["account_id"], run["mode"],
                                on_step=lambda snapshot: self.store.checkpoint(run["id"], self.owner, snapshot))
            self.store.checkpoint(run["id"], self.owner, result, terminal=True)
        except LeaseLost:
            pass
        except (ValueError, RuntimeError) as exc:
            self._fail(run["id"], str(exc))
        except Exception:
            self._fail(run["id"], "Investigation failed unexpectedly. Saved steps remain available; no review ticket was created.")
        return True

    def _fail(self, run_id, error):
        try:
            self.store.checkpoint(run_id, self.owner, {"status": "failed", "error": error}, terminal=True)
        except LeaseLost:
            pass

    def _worker(self):
        while not self.stop.is_set():
            try:
                if not self.run_one():
                    self.stop.wait(0.25)
            except Exception:
                # Storage contention must not kill a worker or log user data.
                self.stop.wait(1)

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=0.25)
