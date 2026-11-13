"""Authenticated loopback-only ticket transport; provider details stay private."""
import http.client
import json
import math
import os
import re
from urllib.parse import urlsplit

MAX_TICKET_BYTES = 131072
ACTIONS = {"billing_review", "engineering_escalation", "identity_review", "support_review"}


def validate_destination(target):
    try:
        parsed = urlsplit(target)
        valid = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
                 and 1 <= parsed.port <= 65535 and not parsed.username and not parsed.password
                 and not parsed.path and not parsed.query and not parsed.fragment
                 and target == f"http://127.0.0.1:{parsed.port}")
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("Ticket sandbox must use http://127.0.0.1:PORT without credentials or a path") from None
    return target


def validate_payload(payload):
    required = {"ticket_id", "run_id", "action", "summary", "reason", "citations"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("Invalid ticket payload")
    if not re.fullmatch(r"TKT-[A-F0-9]{8}", str(payload["ticket_id"])) or not re.fullmatch(r"[a-f0-9]{32}", str(payload["run_id"])) or payload["action"] not in ACTIONS:
        raise ValueError("Invalid ticket reference or action")
    if any(not isinstance(payload[key], str) or (not payload[key].strip() or len(payload[key]) > 4000) for key in ("summary", "reason")):
        raise ValueError("Invalid ticket text")
    refs = payload["citations"]
    if not isinstance(refs, list) or not 1 <= len(refs) <= 20 or any(not isinstance(ref, str) or len(ref) > 80 or not re.fullmatch(r"KB-\d+|ACCOUNT|STATUS-(?:api|exports)", ref) for ref in refs):
        raise ValueError("Invalid ticket citations")
    return payload


class DeliveryError(RuntimeError):
    def __init__(self, code, retryable=False, retry_after=0):
        allowed = {"temporary", "connection", "quota", "access", "invalid_response", "conflict", "request", "internal"}
        self.code = code if code in allowed else "internal"
        self.retryable = bool(retryable)
        try:
            delay = float(retry_after)
            self.retry_after = min(60, max(0, delay)) if math.isfinite(delay) else 0
        except (ValueError, TypeError):
            self.retry_after = 0
        super().__init__("Ticket delivery failed (" + self.code + "); provider details are not exposed.")


class TicketConnector:
    def __init__(self, destination, token):
        self.destination = validate_destination(destination)
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,200}", token):
            raise ValueError("Provide a sandbox token of 16–200 URL-safe characters")
        self.token = token
        self.port = urlsplit(self.destination).port

    @classmethod
    def from_environment(cls):
        target = os.environ.get("RELAY_TICKET_URL", "")
        return cls(target, os.environ.get("RELAY_TICKET_TOKEN", "")) if target else None

    def send(self, payload, idempotency_key):
        validate_payload(payload)
        if idempotency_key != "relay-" + payload["run_id"]:
            raise ValueError("Invalid ticket idempotency key")
        body = json.dumps(payload).encode()
        if len(body) > MAX_TICKET_BYTES:
            raise ValueError("Ticket payload exceeds its wire-size budget")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            # Direct loopback connection: environment proxies and redirects are not used.
            connection.request("POST", "/tickets", body=body, headers={
                "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key})
            response = connection.getresponse()
            status = response.status
            if status not in {200, 201}:
                code = "quota" if status == 429 else "access" if status in {401, 403} else "conflict" if status == 409 else "temporary" if status in {408, 500, 502, 503, 504} else "request"
                raise DeliveryError(code, retryable=status in {408, 429, 500, 502, 503, 504}, retry_after=response.getheader("Retry-After"))
            raw = response.read(8193)
            if len(raw) > 8192:
                raise DeliveryError("invalid_response", retryable=True)
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("status") != "open" or not isinstance(result.get("ticket_id"), str) or not re.fullmatch(r"SBX-[A-F0-9]{12}", result["ticket_id"]):
                raise DeliveryError("invalid_response", retryable=True)
            return result["ticket_id"]
        except (OSError, http.client.HTTPException):
            raise DeliveryError("connection", retryable=True) from None
        except (ValueError, TypeError, UnicodeError):
            raise DeliveryError("invalid_response", retryable=True) from None
        finally:
            connection.close()
