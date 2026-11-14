"""Bounded background workers over the persistent SQLite queue."""
import threading
import uuid
from .agent import investigate, validate_request, provider_config, check_resume_configuration
from .store import LeaseLost, CancelRequested


class JobRunner:
    def __init__(self, store, workers=2, capacity=100, agent=investigate):
        if not 1 <= workers <= 16 or not 1 <= capacity <= 5000:
            raise ValueError("Workers must be 1–16 and queue capacity 1–5000")
        self.store, self.workers, self.capacity, self.agent = store, workers, capacity, agent
        self.owner = uuid.uuid4().hex
        self.stop = threading.Event()
        self.threads = []
        self.active_owners = set()
        self.owners_lock = threading.Lock()

    def submit(self, request, request_key, retry_of=None):
        validate_request(request.get("message"), request.get("account_id"), request.get("mode"))
        if request["mode"] == "live":
            config = provider_config()
            if not config["live_available"]:
                raise ValueError("Live mode requires a configured provider key. Run python3 run_live.py locally.")
            request = {**request, "provider": config["provider"]}
        return self.store.enqueue(request, request_key, self.capacity, retry_of)

    def resume(self, run_id, request_key):
        receipt = self.store.resume_receipt(run_id, request_key)
        if receipt:
            return receipt
        try:
            run = self.store.get(run_id)
            state = self.store.load_agent_checkpoint(run_id)
            if state is None or self.agent is not investigate:
                raise ValueError("No compatible saved live conversation exists. Start a new investigation.")
            check_resume_configuration(run["message"], run["account_id"], state)
        except (ValueError, KeyError):
            # A duplicate may have committed after our initial receipt read.
            receipt = self.store.resume_receipt(run_id, request_key)
            if receipt:
                return receipt
            raise
        return self.store.resume_run(run_id, request_key, self.capacity)

    def cancel(self, run_id):
        return self.store.cancel_run(run_id)

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
                with self.owners_lock:
                    owners = tuple(self.active_owners)
                for owner in owners:
                    self.store.heartbeat(owner)
            except Exception:
                # A prolonged failure expires the lease and fences the worker.
                continue

    def run_one(self):
        config = provider_config()
        owner = self.owner + "-" + uuid.uuid4().hex
        run = self.store.claim(owner, provider=config["provider"] if config["live_available"] else None)
        if not run:
            return False
        with self.owners_lock:
            self.active_owners.add(owner)
        try:
            if self.agent is investigate:
                state = self.store.load_agent_checkpoint(run["id"], owner)
                result = self.agent(run["message"], run["account_id"], run["mode"], resume_state=state,
                                    on_checkpoint=lambda public, private: self.store.checkpoint(run["id"], owner, public, private_state=private),
                                    on_step=None if run["mode"] == "live" else lambda public: self.store.checkpoint(run["id"], owner, public),
                                    should_cancel=lambda: self.store.cancellation_requested(run["id"]))
            else:
                # Legacy injected agents keep their trace-only callback contract.
                result = self.agent(run["message"], run["account_id"], run["mode"],
                                    on_step=lambda public: self.store.checkpoint(run["id"], owner, public))
            self.store.checkpoint(run["id"], owner, result, terminal=True)
        except LeaseLost:
            pass
        except CancelRequested:
            self._cancel(run["id"], owner)
        except (ValueError, RuntimeError) as exc:
            self._fail(run["id"], owner, str(exc))
        except Exception:
            self._fail(run["id"], owner, "Investigation failed unexpectedly. Saved steps remain available; no review ticket was created.")
        finally:
            with self.owners_lock:
                self.active_owners.discard(owner)
        return True

    def _cancel(self, run_id, owner):
        try:
            self.store.finish_cancel(run_id, owner)
        except LeaseLost:
            pass

    def _fail(self, run_id, owner, error):
        if self.store.cancellation_requested(run_id):
            self._cancel(run_id, owner)
            return
        try:
            self.store.checkpoint(run_id, owner, {"status": "failed", "error": error}, terminal=True)
        except CancelRequested:
            self._cancel(run_id, owner)
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
