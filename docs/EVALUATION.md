# Agent quality evaluation

The quality suite contains 70 synthetic English scenarios labeled against Relay's supplied account, service, and policy fixtures: 35 development cases and 35 test cases. Each split covers billing, API incidents, account access, exports, cancellation, unclear issues, and adversarial instructions.

The labels are authored expectations, not independently validated customer outcomes. Public test cases remain a holdout only while they are excluded from prompt/model tuning. Use the development split during iteration and reserve the test split for a frozen configuration. Do not tune against a test failure and then describe a rerun on the same cases as an untouched holdout result.

## Commands

Validate the dataset without contacting a provider:

```sh
python3 evals.py --validate
```

Run the deterministic development baseline:

```sh
python3 evals.py --suite quality --split dev --output reports/demo-dev.json
```

Run a small live smoke evaluation using a hidden key prompt:

```sh
python3 evals.py --suite quality --live --provider gemini --prompt-key --split dev --limit 3 --output reports/live-dev-smoke.json
```

After reviewing development behavior and freezing the configuration, run the test split:

```sh
python3 evals.py --suite quality --live --provider gemini --prompt-key --split test --output reports/live-test.json
```

`--repeat 2` makes two independent investigations per selected case. Live evaluations consume provider quota and may incur charges according to the account's plan. Rate-limit/quota errors stop the suite immediately; three consecutive execution errors also stop it. Remaining cases are marked as unattempted through the report's planned/attempted totals.

The original `python3 evals.py` command retains the ten-case deterministic regression suite used by CI. Quality reports never overwrite existing results. Use a new output filename for each run.

## Automatic scoring

| Check | Measurement |
| --- | --- |
| Action | Proposed action belongs to the case's accepted actions |
| Tool coverage | Successful calls include required tools and the correct service argument |
| Tool validity | Calls use supported tools and pass argument validation |
| Citation coverage | All labeled evidence IDs appear in the final citations |
| Citation validity | Citations refer to evidence retrieved during that investigation |
| Verification boundary | The proposal passed verification and awaits approval without a ticket |
| Response text | Expected fixture terms occur and selected prohibited claim patterns do not |
| Clarification proxy | Cases with missing information contain a request for more details |
| Abstention proxy | Unclear cases contain an uncertainty or information-request expression |

Execution failures count against automatic rates rather than being discarded. Clarification and abstention rates use only cases where the respective behavior is labeled necessary; when no cases apply, the rate is null. Reports also record invalid tool attempts, wall-clock latency, p95 latency, and returned token totals. Failed requests may have incomplete token accounting. Token totals are not monetary cost.

English text patterns are proxies. They do not establish semantic correctness, factual support for every claim, or comprehensive resistance to injected instructions. Automatic pass rate must not be presented as general AI answer accuracy.

## Human review

Each JSON report has a companion `.review.csv` file containing case inputs, answers, expected actions, evidence, and automatic failures. Annotate the following columns with `yes`, `no`, `na`, or leave them blank:

- `groundedness_correct`: Every material factual claim is supported by the supplied evidence. Unsupported causes, invented facts, and unsupported commitments receive `no`.
- `resolution_useful`: The response addresses the issue and gives an appropriate, actionable next step without promising an unexecuted action.
- `abstention_appropriate`: The response acknowledges missing information when necessary and asks useful questions. Use `na` where uncertainty handling is not applicable.
- `review_comments`: Optional explanation of the judgment.

Do not edit case IDs, repeat numbers, or answer text. Evaluate complete answers only. For higher-confidence labels, use independent reviewers and resolve disagreements before publishing semantic-quality results.

```sh
python3 scripts/review_quality.py reports/live-test.json reports/live-test.review.csv
```

Human rates are reported separately from automatic rates, with review coverage. Blank/unreviewed answers are not counted as correct. A rate based on partial review is not a full-suite result.

## Reproducibility and data handling

Reports identify the dataset version/hash, prompt hash, engine/grader hashes, fixture hashes, selected split, subset status, provider, configured model, returned model, repeat number, and UTC run timestamp. Run the same frozen configuration when comparing models or prompts. Do not select only successful cases for comparison.

Generated reports contain answers and traces and stay in the ignored `reports/` directory. Provider credentials and private conversation metadata are not included. Do not commit live result artifacts or review files to the public repository. Published source includes only the synthetic case dataset and evaluation tooling.
