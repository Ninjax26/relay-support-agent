# Activate Gemini live mode

You need a Gemini API key from your own Google AI Studio account. Do not paste it into chat, screenshots, or source files.

```sh
cd relay-support-agent
PORT=8001 python3 run_live.py
```

1. Paste the key into the hidden terminal prompt. It stays in the server process environment and is not written to disk.
2. Open http://127.0.0.1:8001, leaving the existing demo on port 8000 available.
3. Select **Live LLM**, then investigate a fictional issue.
4. Inspect the plan, evidence, verification, and tool trace. The model now selects the steps.

If you already configured `GEMINI_API_KEY` in the terminal environment, the launcher skips the prompt. The default model is `gemini-3.8-flash`; override with `GEMINI_MODEL` for another supported tool-calling model accessible to your account. The `.env.example` file is reference only; it is not automatically loaded.

The adapter uses [Google's documented compatibility endpoint](https://ai.google.dev/gemini-api/docs/openai) and replays full assistant tool-call metadata, including thought-signature metadata when returned. It does not display private reasoning in the UI.

## Free tier

Google offers free usage for selected models, with quotas and eligibility constraints. Check your project's current limits in AI Studio and the [pricing page](https://ai.google.dev/gemini-api/docs/pricing). Quota failures produce a retry/check-limits message; Relay does not silently switch providers or enable billing. Free-tier data may be used to improve Google's products. Use the provided fictional scenarios.

## Evaluation and alternative provider

After configuring environment credentials, run `python3 evals.py --live --limit 1` for one live routing/evidence check. The hidden launcher prompt configures only its own process, so a separate evaluation process needs its own environment credentials. Live evaluation is subject to the provider's quota/pricing and does not grade every factual claim.

OpenAI remains available with `PORT=8001 python3 run_live.py --provider openai`. Set `AGENT_PROVIDER` explicitly when both providers' environment credentials exist; otherwise Gemini is selected when its key is present.

## HTTP 503 / temporarily unavailable

Google documents 503 as temporary service overload/unavailability. Relay retries HTTP 500/502/503/504 up to three total attempts with exponential backoff and jitter, and honors numeric Retry-After values up to a bounded 10-second delay. Authentication and quota errors are distinguished and are not retried automatically. Only the model request is retried; local tool actions are never replayed by this retry helper.

After updating code, stop the live server with Ctrl+C and restart `PORT=8001 python3 run_live.py`; re-enter your key locally if prompted. If three attempts still fail, wait before trying again or explicitly configure another supported GEMINI_MODEL. A 503 alone does not establish that the key is invalid. See [Google troubleshooting](https://ai.google.dev/gemini-api/docs/troubleshooting).

## Connection diagnosis

Run `python3 run_live.py --check` from the project directory and enter your key in the hidden prompt. This makes a small authenticated model/tool-call request and exits without creating tickets or executing tools. It consumes provider quota. The command reports CHECK PASSED or CHECK FAILED without exposing credentials.

Relay distinguishes response timeouts, DNS failures, TLS verification failures, and other connection failures. Gemini requests allow 60 seconds by default; GEMINI_TIMEOUT_SECONDS accepts values from 5 to 60. This is a per-request timeout, not a hard whole-investigation deadline. A running server must be restarted to load source changes.
