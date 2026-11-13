"""Private, versioned live-agent state. Never include it in public run payloads."""
import copy
import hashlib
import json
import math
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_CHECKPOINT_BYTES = 2_000_000
ENGINE_SHA256 = hashlib.sha256(b"".join((ROOT / "supportops" / name).read_bytes() for name in
    ("agent.py", "tools.py", "verification.py", "retrieval.py", "embeddings.py", "jobs.py", "store.py", "checkpoints.py"))).hexdigest()
TOOL_STATE = ("evidence", "plan", "plan_history", "verification", "verified_proposal", "retrieval_events")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def make_binding(message, account_id, provider, model, tools, prompt):
    return {"request": digest({"message": message, "account_id": account_id, "mode": "live"}),
            "provider": provider, "model": model, "retrieval_mode": tools.retriever.mode,
            "prompt": digest(prompt), "engine": ENGINE_SHA256,
            "fixtures": digest([tools.docs, tools.accounts, tools.services])}


def pack_state(binding, history, trace, usage, tools, model_requests, elapsed_ms, answer=None, inflight_started_at=None):
    state = {"version": 1, "binding": binding, "history": history, "trace": trace, "usage": usage,
             "tools": {key: getattr(tools, key) for key in TOOL_STATE}, "model_requests": model_requests,
             "elapsed_ms": elapsed_ms, "answer": answer, "inflight_started_at": inflight_started_at}
    validate_state(state)
    return copy.deepcopy(state)


def validate_state(state, expected_binding=None):
    try:
        if len(json.dumps(state, allow_nan=False).encode()) > MAX_CHECKPOINT_BYTES:
            raise ValueError()
        if not isinstance(state, dict) or set(state) != {"version", "binding", "history", "trace", "usage", "tools", "model_requests", "elapsed_ms", "answer", "inflight_started_at"} or state["version"] != 1:
            raise ValueError()
        if not isinstance(state["binding"], dict) or not isinstance(state["history"], list) or not 1 <= len(state["history"]) <= 128 or not all(isinstance(item, dict) for item in state["history"]):
            raise ValueError()
        if not isinstance(state["trace"], list) or len(state["trace"]) > 16 or not all(isinstance(item, dict) for item in state["trace"]):
            raise ValueError()
        if type(state["model_requests"]) is not int or not len(state["trace"]) <= state["model_requests"] <= 16 or type(state["elapsed_ms"]) is not int or state["elapsed_ms"] < 0:
            raise ValueError()
        pending = state["inflight_started_at"]
        if pending is not None and (type(pending) not in (int, float) or not math.isfinite(pending) or pending < 0):
            raise ValueError()
        if not isinstance(state["usage"], dict) or set(state["usage"]) != {"input_tokens", "output_tokens"} or any(type(v) is not int or v < 0 for v in state["usage"].values()):
            raise ValueError()
        if not isinstance(state["tools"], dict) or set(state["tools"]) != set(TOOL_STATE) or not isinstance(state["tools"]["evidence"], dict) or not isinstance(state["tools"]["plan_history"], list) or not isinstance(state["tools"]["retrieval_events"], list):
            raise ValueError()
        if state["trace"] and (state["history"][-1].get("type") != "function_call_output"):
            raise ValueError()
        if state["answer"] is not None and (not state["trace"] or state["trace"][-1].get("tool") != "complete_case" or not state["trace"][-1].get("ok") or state["trace"][-1].get("result") != state["answer"]):
            raise ValueError()
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ValueError("Saved conversation is invalid or too large. Start a new investigation.") from None
    if expected_binding is not None and state["binding"] != expected_binding:
        raise ValueError("Saved conversation does not match the current request, provider, model, code or source data. Restore its original configuration or start a new investigation.")
    return state


def restore_tools(tools, state):
    for key in TOOL_STATE:
        setattr(tools, key, copy.deepcopy(state["tools"][key]))


def active_elapsed_ms(state):
    pending = state["inflight_started_at"]
    ambiguous_ms = min(180000, max(0, round((time.time() - pending) * 1000))) if pending is not None else 0
    return state["elapsed_ms"] + ambiguous_ms


def resumable(state):
    return state["answer"] is not None or (len(state["trace"]) < 16 and state["model_requests"] < 16 and active_elapsed_ms(state) < 180000)
