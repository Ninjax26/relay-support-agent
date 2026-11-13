"""Run Relay with an isolated authenticated local ticket service."""
import argparse
import getpass
import os
import secrets
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import create_server
from supportops.ticketing import TicketConnector
from supportops.ticket_sandbox import SandboxStore, create_ticket_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8012)
    parser.add_argument("--ticket-port", type=int, default=8014)
    parser.add_argument("--live", action="store_true", help="Prompt for a generation key if needed; demo investigations remain offline")
    parser.add_argument("--provider", choices=["gemini", "openai"], default="gemini")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.ticket_port <= 65535 or args.port == args.ticket_port:
        parser.error("Choose distinct Relay and ticket ports between 1 and 65535")
    if args.live:
        os.environ["AGENT_PROVIDER"] = args.provider
        variable = "GEMINI_API_KEY" if args.provider == "gemini" else "OPENAI_API_KEY"
        if not os.environ.get(variable):
            if not sys.stdin.isatty():
                parser.error("Enter the provider key in an interactive terminal")
            key = getpass.getpass(f"{args.provider.title()} API key (hidden; process only): ").strip()
            if not key:
                parser.error("No key entered")
            os.environ[variable] = key
    token = secrets.token_urlsafe(32)
    service = create_ticket_server(SandboxStore(ROOT / "data/ticket-sandbox.sqlite3"), token, args.ticket_port)
    app = None
    thread = None
    try:
        app = create_server(args.port, ticket_connector=TicketConnector(f"http://127.0.0.1:{args.ticket_port}", token))
        thread = threading.Thread(target=service.serve_forever, name="relay-ticket-sandbox", daemon=True)
        thread.start()
        print(f"Relay: http://127.0.0.1:{args.port}", flush=True)
        print(f"Local test ticket service: port {args.ticket_port}; credential held in memory. Approval queues delivery; no customer account or payment is changed.", flush=True)
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app:
            app.server_close()
        if thread:
            service.shutdown()
            thread.join(timeout=2)
        service.server_close()


if __name__ == "__main__":
    main()
