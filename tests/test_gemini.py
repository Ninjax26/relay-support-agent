import io
import json
import os
import socket
import ssl
import unittest
import urllib.error
from unittest.mock import patch
from supportops.agent import GeminiPlanner, provider_config


class GeminiTests(unittest.TestCase):
    def test_tool_metadata_replayed_and_usage_normalized(self):
        raw_message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "update_plan", "arguments": '{"objective":"Investigate","steps":["Search"],"missing_information":[]}'}, "extra_content": {"google": {"thought_signature": "opaque-test-signature"}}}]}
        payload = {"choices": [{"message": raw_message}], "usage": {"prompt_tokens": 20, "completion_tokens": 8}}
        requests = []

        def respond(request, timeout):
            requests.append(json.loads(request.data))
            return io.BytesIO(json.dumps(payload).encode())

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-key"}, clear=True), patch("urllib.request.urlopen", side_effect=respond):
            planner = GeminiPlanner()
            history = [{"role": "user", "content": "API timeout"}]
            first = planner.respond(history)
            self.assertEqual(first["usage"], {"input_tokens": 20, "output_tokens": 8})
            history.extend(first["output"])
            history.append({"type": "function_call_output", "call_id": "call-1", "output": '{"recorded":true}'})
            planner.respond(history)
        self.assertEqual(requests[1]["messages"][2], raw_message)
        self.assertEqual(requests[1]["messages"][3]["tool_call_id"], "call-1")
        self.assertNotIn("test-only-key", json.dumps(requests))
        self.assertEqual(requests[0]["tools"][0]["function"]["name"], "update_plan")

    def test_quota_error_is_actionable_without_provider_payload(self):
        error = urllib.error.HTTPError("https://example.invalid", 429, "quota", {}, io.BytesIO(b"private provider details"))
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-key"}, clear=True), patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "rate limit or quota"):
                GeminiPlanner().respond([{"role": "user", "content": "help"}])

    def test_empty_tool_response_fails_closed(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-key"}, clear=True), patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"choices":[{"message":{"role":"assistant","content":"hello"}}]}')):
            with self.assertRaisesRegex(RuntimeError, "usable tool call"):
                GeminiPlanner().respond([{"role": "user", "content": "help"}])

    def test_transient_failure_retries_same_request_then_recovers(self):
        failure = urllib.error.HTTPError("https://example.invalid", 503, "overloaded", {"Retry-After": "2"}, io.BytesIO(b"private details"))
        success = io.BytesIO(b'{"recovered":true}')
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-key"}, clear=True), patch("urllib.request.urlopen", side_effect=[failure, success]) as send, patch("time.sleep") as sleep:
            request = object()
            self.assertEqual(GeminiPlanner().request_json(request), {"recovered": True})
            self.assertEqual(send.call_count, 2)
            self.assertIs(send.call_args_list[0].args[0], send.call_args_list[1].args[0])
            self.assertEqual(sleep.call_args.args[0], 2)

    def test_repeated_503_stops_after_three_attempts(self):
        failures = [urllib.error.HTTPError("https://example.invalid", 503, "overloaded", {}, io.BytesIO(b"private details")) for _ in range(3)]
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-key"}, clear=True), patch("urllib.request.urlopen", side_effect=failures) as send, patch("time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "temporarily unavailable.*after 3 attempts") as raised:
                GeminiPlanner().request_json(object())
            self.assertNotIn("private details", str(raised.exception))
            self.assertEqual(send.call_count, 3)
            self.assertEqual(sleep.call_count, 2)

    def test_authentication_errors_are_not_retried(self):
        failure = urllib.error.HTTPError("https://example.invalid", 403, "denied", {}, io.BytesIO(b"private details"))
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-key"}, clear=True), patch("urllib.request.urlopen", side_effect=failure) as send, patch("time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "authentication or permission"):
                GeminiPlanner().request_json(object())
            self.assertEqual(send.call_count, 1)
            sleep.assert_not_called()

    def test_connection_failure_types_have_distinct_messages(self):
        cases = [(TimeoutError(), "did not respond within 60 seconds"), (socket.gaierror(), "DNS lookup"), (ssl.SSLCertVerificationError(), "verified TLS"), (ConnectionRefusedError(), "connection to Gemini failed")]
        for reason, expected in cases:
            with self.subTest(reason=type(reason).__name__), patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-key"}, clear=True), patch("urllib.request.urlopen", side_effect=urllib.error.URLError(reason)):
                with self.assertRaisesRegex(RuntimeError, expected):
                    GeminiPlanner().request_json(object())

    def test_timeout_configuration_is_bounded(self):
        for value in ["abc", "0", "61", "nan"]:
            with self.subTest(value=value), patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-key", "GEMINI_TIMEOUT_SECONDS": value}, clear=True):
                with self.assertRaises(ValueError):
                    GeminiPlanner()

    def test_provider_selection_and_public_config(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-key", "OPENAI_API_KEY": "another-test-key"}, clear=True):
            self.assertEqual(provider_config(), {"provider": "gemini", "live_available": True})
            os.environ["AGENT_PROVIDER"] = "openai"
            self.assertEqual(provider_config()["provider"], "openai")
        with patch.dict(os.environ, {"AGENT_PROVIDER": "gemini"}, clear=True):
            self.assertFalse(provider_config()["live_available"])
            with self.assertRaisesRegex(ValueError, "GEMINI_API_KEY"):
                GeminiPlanner()


if __name__ == "__main__":
    unittest.main()
