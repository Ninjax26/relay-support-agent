"""Bounded background workers over the persistent SQLite queue."""
import hashlib
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

    def submit(self, request, request_key, retry_of=None, user_id=None):
        validate_request(request.get("message"), request.get("account_id"), request.get("mode"))
        if request["mode"] == "live":
            config = provider_config()
            if not config["live_available"]:
                raise ValueError("Live mode requires a configured provider key. Check the Relay deployment settings.")
            request = {**request, "provider": config["provider"]}
        return self.store.enqueue(request, request_key, self.capacity, retry_of, user_id)

    def resume(self, run_id, request_key, user_id=None):
        if user_id is not None:
            if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
                raise ValueError("Provide a request_id of 1–128 characters")
            self.store.require_owner(run_id, user_id)
            request_key = hashlib.sha256((user_id + ":" + request_key).encode()).hexdigest()
        receipt = self.store.resume_receipt(run_id, request_key)
        if receipt:
            return receipt
        try:
            run = self.store.get(run_id)
            state = self.store.load_agent_checkpoint(run_id)
            if state is None or self.agent is not investigate:
                raise ValueError("No compatible saved live conversation exists. Start a new investigation.")
            check_resume_configuration(run["message"], run["account_id"], state, self.store.run_policies(run_id))
        except (ValueError, KeyError):
            # A duplicate may have committed after our initial receipt read.
            receipt = self.store.resume_receipt(run_id, request_key)
            if receipt:
                return receipt
            raise
        return self.store.resume_run(run_id, request_key, self.capacity, user_id)

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

    def run_one(self, only_run=None, lease_seconds=90, concurrency_limit=None):
        config = provider_config()
        owner = self.owner + "-" + uuid.uuid4().hex
        run = self.store.claim(owner, lease_seconds=lease_seconds,
                               provider=config["provider"] if config["live_available"] else None,
                               only_run=only_run, concurrency_limit=concurrency_limit)
        if not run:
            return False
        with self.owners_lock:
            self.active_owners.add(owner)
        attempt_stop = threading.Event()
        heartbeat = None
        if only_run:
            heartbeat = threading.Thread(target=self.heartbeat_attempt, args=(owner, attempt_stop), daemon=True)
            heartbeat.start()
        try:
            if self.agent is investigate:
                state = self.store.load_agent_checkpoint(run["id"], owner)
                result = self.agent(run["message"], run["account_id"], run["mode"], resume_state=state,
                                    on_checkpoint=lambda public, private: self.store.checkpoint(run["id"], owner, public, private_state=private),
                                    on_step=None if run["mode"] == "live" else lambda public: self.store.checkpoint(run["id"], owner, public),
                                    should_cancel=lambda: self.store.cancellation_requested(run["id"]),
                                    workspace_policies=self.store.run_policies(run["id"]) if run.get("policy_count") else None)
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
            attempt_stop.set()
            if heartbeat:
                heartbeat.join(timeout=1)
            with self.owners_lock:
                self.active_owners.discard(owner)
        return True

    def heartbeat_attempt(self, owner, stop):
        while not stop.wait(10):
            try:
                self.store.heartbeat(owner)
            except Exception:
                continue

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
