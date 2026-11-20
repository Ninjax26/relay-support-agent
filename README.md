# Relay · Agentic support investigations

[Open the live app](https://relay-support-desk-ruby.vercel.app) · [Execution workflow](docs/WORKFLOW.md) · [Evaluation](docs/EVALUATION.md)

[![Agent checks](https://github.com/Ninjax26/relay-support-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Ninjax26/relay-support-agent/actions/workflows/ci.yml)

Relay turns a support complaint into a cited recommendation and a review ticket. A model chooses which tools to call, reads the resulting evidence, and revises the draft when verification fails. A person approves or rejects the proposal. The hosted app runs on Vercel with PostgreSQL, Google sign-in, and separate policy libraries and case histories for each account.

## Try a policy-based investigation

1. Open the live app and sign in with Google or create a Relay account. Google sign-in and password sign-in each have their own workspace.
2. Expand **Workspace policies**. Paste the sample below or upload it as a UTF-8 `.txt` or `.md` file. Save it with the title **Duplicate charge playbook**.
3. Select **Live AI · Gemini** and **Use my workspace policies**. Choose **Northstar Labs** and enter this issue. “Two payments appeared for invoice INV-204. What should I provide for the billing review?”
4. Open the policy citation and trace. Check the ledger lookup, the policy search and the verification result. Select **Approve review ticket** to save the decision and ticket in your case history.

> For duplicate payments against the same invoice, ask the customer to provide their purchase-order reference for billing review. Keep the decision pending until a reviewer approves the proposal.

The public workspace uses fictional customer records and shares a bounded allowance for live investigations. **Demo** mode shows the same tools and approval flow with a scripted planner. It attaches a matching policy as evidence. In **Live AI** mode, the model receives retrieved policy text and can use it to adapt the reply.

## Run locally

Python 3.12 is used in CI. The local demo works offline with Python’s standard library.

~~~sh
git clone https://github.com/Ninjax26/relay-support-agent.git
cd relay-support-agent
python3 server.py
~~~

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and run a sample case.

To try account sign-in and policy uploads locally, start a separate authenticated SQLite workspace:

~~~sh
RELAY_PUBLIC_ORIGIN=http://127.0.0.1:8000 RELAY_BIND=127.0.0.1 \
RELAY_DB_PATH=data/workspaces.sqlite3 PORT=8000 python3 server.py
~~~

Use the `127.0.0.1` address above and create a password account. For local Gemini, run the same command with `python3 run_live.py` and enter the key at the hidden prompt. Set `GEMINI_MODEL` to a tool-calling model available to your project. The launcher also supports `--provider openai` and a one-request `--check`. See [provider configuration](docs/EXTERNAL_SETUP.md).

## How it works

~~~mermaid
flowchart LR
    A[Customer issue] --> B[Plan and select tools]
    B --> C[Read policies and account evidence]
    C --> D[Verify the cited proposal]
    D -->|Revise| B
    D -->|Pass| E[Human review]
    E -->|Approve| F[Review ticket]
~~~

- **Retrieval and verification.** BM25 searches the built-in playbooks and the workspace’s selected policy library. The built-in corpus can use optional Gemini embeddings and hybrid ranking. Verification checks required citations and recognized claims, and binds approval to the exact proposal.
- **Persistent investigations.** A queued run snapshots the selected workspace policies. The worker saves progress and private conversation checkpoints. A compatible failed live run can resume. A new retry uses the original policy snapshot. Local progress uses server-sent events and hosted progress uses polling.
- **Review and delivery.** Approval is idempotent. The hosted app stores the resulting ticket in Relay. A separate local sandbox can deliver approved proposals to an isolated HTTP ticket service. See [ticket delivery](docs/TICKETING.md).

## Host your own instance

The Vercel entry point is `app.py` and its build configuration is `vercel.json`. Install the hosted dependencies from `requirements.txt` when running that entry point locally.

| Environment variable | Purpose |
| --- | --- |
| `DATABASE_URL` or `POSTGRES_URL` | PostgreSQL connection string |
| `RELAY_PUBLIC_ORIGIN` | Exact HTTPS app origin when using a custom domain |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | Server-side Gemini credential and model for Live AI |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Optional Google OAuth Web application client |
| `RELAY_DAILY_LIVE_LIMIT` | Shared cap on live starts and resumes in a rolling day, default 8 |

Register the exact URL `<origin>/api/session/google/callback` on the Google Web client. Keep credentials in your hosting provider’s environment settings. See [.env.example](.env.example) for the configuration names.

## Checks and evaluation

~~~sh
python3 -m unittest discover -s tests -v
python3 evals.py
python3 evals.py --validate
python3 evals.py --suite quality --split dev --output reports/demo-dev.json
python3 scripts/check_publish.py --tracked
~~~

The quality dataset has 70 synthetic cases split evenly into development and held-out sets. The automatic checks cover routing, tool use, citations and approval boundaries. Semantic accuracy requires a separate human review. Generated reports are ignored by Git. See [evaluation details](docs/EVALUATION.md), [verification rules](docs/VERIFICATION.md) and [conversation recovery](docs/RECOVERY.md).
