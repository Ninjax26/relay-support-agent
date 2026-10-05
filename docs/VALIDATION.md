# Validation evidence

Validated locally on September 30, 2026 using Python 3.14, Node 22, and the Codex in-app browser.

- `python3 -m unittest discover -s tests -v`: 16 tests passed. Covers tool boundaries, citation validation, fake-provider live-loop feedback/recovery, execution budget, scenario behavior, and transactional/idempotent approval.
- `python3 evals.py`: 10/10 deterministic demo cases passed. This is a regression measure, not live-model accuracy.
- `node --check web/app.js`: passed.
- Browser: duplicate-charge investigation rendered reply, source IDs, and tool trace; approval produced a local ticket ID and disabled repeat-decision controls.
- Browser: API timeout investigation rendered incident INC-042 and service-status evidence; rejection showed that no ticket was created.
- Desktop layout inspected at 1440 × 1050. At a 320px viewport the DOM-reported page width was 305px, with no horizontal overflow.
- Browser console inspection returned no captured errors or warnings.
- Design-hook text-size findings were fixed by enlarging compact text and body content. No ignores or suppressions were added.

Not validated: real provider API calls, live-model answer quality, screen-reader behavior, full keyboard workflow, 200% zoom, cross-browser behavior, real customer integrations, or public deployment.

Agent upgrade validation: planning and revision state, verification failure feedback followed by evidence retrieval, exact-proposal binding, invalidation after new evidence, and unknown identifier/action-claim rejection are covered by tests. Browser inspection confirmed a six-step demo with update_plan, retrieval, account lookup, plan revision, verify_case, and complete_case; the plan and verification report are displayed. Live model execution remains pending API credentials.

Gemini adapter: 20 total unit tests pass. Added mocked HTTP contract tests for tool metadata replay, normalized usage, provider selection, quota failures, and non-tool responses. Real Gemini API calls remain unverified.

Transient-error handling: 23 unit tests pass. Added 503-then-success retry, repeated-503 exhaustion, and non-retryable authentication tests using mocked HTTP responses; no live outage recovery has been verified.

Connection diagnosis: 25 tests pass. Mocked DNS, TLS, timeout, and generic connection failures produce distinct messages; timeout configuration is bounded. An unauthenticated host connectivity check outside the restricted tool sandbox resolved both Google hosts and received HTTPS responses (API root 404, docs root 200). This proves reachability at check time, not authenticated model availability.
