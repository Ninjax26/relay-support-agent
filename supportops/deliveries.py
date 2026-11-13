"""Durable approved-ticket delivery with bounded retries and fenced acknowledgments."""
import threading
import uuid
from .store import LeaseLost
from .ticketing import DeliveryError


class DeliveryRunner:
    def __init__(self, store, connector):
        self.store, self.connector = store, connector
        self.stop = threading.Event()
        self.thread = None

    def run_one(self):
        owner = uuid.uuid4().hex
        job = self.store.claim_delivery(owner, self.connector.destination)
        if not job:
            return False
        try:
            external_id = self.connector.send(job["payload"], job["idempotency_key"])
            self.store.finish_delivery(job["ticket_id"], owner, external_id=external_id)
        except LeaseLost:
            pass
        except DeliveryError as exc:
            self._fail(job, owner, exc.code, exc.retryable, exc.retry_after)
        except Exception:
            self._fail(job, owner, "internal", False, 0)
        return True

    def _fail(self, job, owner, code, retryable, retry_after):
        try:
            self.store.finish_delivery(job["ticket_id"], owner, error_code=code,
                                       retryable=retryable, retry_after=retry_after)
        except LeaseLost:
            pass

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self._work, name="relay-ticket-delivery", daemon=True)
        self.thread.start()

    def _work(self):
        while not self.stop.is_set():
            try:
                if not self.run_one():
                    self.stop.wait(.25)
            except Exception:
                self.stop.wait(1)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=6)
