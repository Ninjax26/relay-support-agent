"""Start live mode with a hidden key prompt; never writes credentials to disk."""
import getpass
import argparse
import os
import runpy
from pathlib import Path
from supportops.agent import GeminiPlanner, OpenAIPlanner

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start Relay with a hidden local API-key prompt")
    parser.add_argument("--provider", choices=["gemini", "openai"], default="gemini")
    parser.add_argument("--check", action="store_true", help="Make one small authenticated tool-call request and exit without starting the server")
    args = parser.parse_args()
    os.environ["AGENT_PROVIDER"] = args.provider
    variable = "GEMINI_API_KEY" if args.provider == "gemini" else "OPENAI_API_KEY"
    if not os.environ.get(variable):
        key = getpass.getpass(f"{args.provider.title()} API key (hidden, used only for this process): ").strip()
        if not key:
            raise SystemExit("No key entered. Use python3 server.py for demo mode.")
        os.environ[variable] = key
    print(f"{args.provider.title()} live mode enabled. Requests and tool results will be sent to this provider. Usage is subject to your account's pricing and quota.")
    if args.check:
        planner = GeminiPlanner() if args.provider == "gemini" else OpenAIPlanner()
        try:
            response = planner.respond([{"role": "user", "content": "Connection check only. Call update_plan with objective 'Connection check', steps ['Check provider'], and empty missing_information. Do not investigate or execute other actions."}])
            calls = [item for item in response.get("output", []) if item.get("type") == "function_call"]
            if not calls:
                raise RuntimeError("Provider responded but did not return a tool call")
            print(f"CHECK PASSED: {planner.model} returned a tool call. No local tools were executed or tickets created.")
        except (ValueError, RuntimeError) as exc:
            raise SystemExit(f"CHECK FAILED: {exc}") from None
        raise SystemExit(0)
    runpy.run_path(str(Path(__file__).parent / "server.py"), run_name="__main__")
