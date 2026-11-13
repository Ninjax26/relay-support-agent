# Evidence consistency verification

Before a proposal reaches human review, `verify_case` checks both the customer reply (`summary`) and the internal review justification (`reason`). Checks run locally without a model call or additional credentials.

## Workflow

1. The agent retrieves playbooks and scoped account or service records.
2. It submits a draft with citations and a proposed review action.
3. The backend selects **only cited, retrieved records** for consistency checks. Merely retrieving another record does not support the draft.
4. Recognized contradictions return `passed: false`, human-readable `issues`, and structured `findings` containing a rule code, proposal field, matched claim, relevant source IDs, and corrective feedback. Source IDs in a failed finding identify needed evidence; they do not mean that evidence was cited.
5. In live mode, the model can collect evidence or revise the draft and verify again within the existing sixteen-call budget. Failed verification clears completion permission; `complete_case` rejects a failed or changed draft.
6. A passing draft requires human approval before one local review ticket can be created. No refund, account unlock, cancellation, or export rerun is executed.

## Supported checks

| Area | Comparison |
| --- | --- |
| Identifiers | Invoice IDs must occur in cited account payments; incident IDs must occur in cited service records. Matching ignores case. Playbook text cannot establish an observed invoice or incident. |
| Money | Recognized currency-prefixed/suffixed amounts and dollar/euro/pound symbols must match ledger values. An explicit invoice limits the comparison to that invoice. Explicit totals use settled payments in that scope. |
| Payment counts | Recognized settled-payment counts must match the ledger; a stated currency and amount restrict the counted rows. Recognized duplicate assertions/denials are compared with settled invoice counts. |
| Account | Recognized current plan and account-lock assertions must match the cited account record. |
| Export eligibility | Recognized eligibility statements require ACCOUNT and KB-104; the fixture policy permits Pro and Business. |
| Service | Recognized API/export state assertions require the matching status record; `healthy` maps to `operational`. Region assertions must match the cited service/incident. |
| Action policy | Recognized assertions that a refund, unlock, cancellation, or rerun was executed are rejected. Recognized refund guarantees are rejected. Explicit negative statements such as “A refund has not been issued” remain allowed. |
| Recovery | Recognized restoration promises and recovery deadlines are rejected because the current structured sources expose no recovery estimate. |

For example, “USD 500 against INV-204” is rejected when the cited ledger contains USD 49 payments. “API service is operational” is rejected when STATUS-api says degraded. “INC-042” cannot be supported by an uncited status record even if the agent retrieved it.

## Scope and limits

This is a deterministic English rule set, **not a general fact-checker or semantic entailment model**. It recognizes defined sentence patterns. Unsupported paraphrases, quotations, negation scope, other languages, causality, policy interpretation, mixed-service sentences, ambiguous invoice associations, and statements outside those patterns may escape checks or produce false positives. Money recognition covers common currency codes and $, €, £; other notation may not be recognized. Fixtures are fictional snapshots, not fresh production observations. Rules for export eligibility must be updated if the policy changes.

A passed result means that the implemented rules found no violation. It does not certify every sentence. Human review and independent live groundedness evaluation remain necessary. The quality report fingerprints the verifier alongside the engine, prompt, and fixtures so runs using different rules can be distinguished.

## Validation

```bash
python3 -m unittest discover -s tests -q
python3 evals.py
python3 evals.py --suite quality --split dev
```

Adversarial tests exercise fabricated amounts/currencies/counts, invoice scope, false account/service facts, uncited identifiers, claims hidden in the review reason, positive and negative action statements, completion blocking, and a simulated live planner correcting its draft after feedback. These are offline control-flow and rule checks; they do not measure live-model accuracy. Keep the reserved test split out of rule tuning.
