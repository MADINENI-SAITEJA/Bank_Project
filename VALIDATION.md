# BankFlow validation and test evidence

## Ubuntu VM verification

- The Docker Compose deployment ran successfully on Ubuntu. All 58 tests passed
  with zero failures, errors, or skips. The live HTTP acceptance check passed for
  ledger operations, retries, reversals, inference, reviews, and the API contract.
- The VM pipeline ran real Ray Data preparation, passed the model quality gate,
  and served the approved model. The test report, pipeline results, and resource
  measurements are in [evidence/vm/](evidence/vm/).
- Prometheus configuration and rule checks passed. All five service probes
  passed, all eight scrape targets were UP, and Grafana displayed live metrics.
- `python3 verify.py --lifecycle` exited with code 0. It verified drift-triggered
  retraining and deployment, quality-gate rejection, rollback, outage handling,
  and Prometheus alert recovery. [Lifecycle log](evidence/vm/lifecycle.log)
- Firefox rendered Overview at 320 × 480 with all three navigation tabs visible.

## Engineering changes from the original ZIP audit

- Replaced substring queue rules with issue-token rules. Top-up requests and
  swallowed-card/ATM issues now reach their intended queues.
- Human reviews expose an effective corrected queue while keeping the original
  category, route and score for audit.
- Added the automated test stage to the DVC graph and made training depend on
  its successful report. UI/manual/automatic pipeline paths all use it.
- Added typed response schemas and strict model-output validation, including
  finite bounded scores, boolean review flags, valid run IDs and category/queue
  consistency. Rejected predictions never create tickets.
- SQLite connections close explicitly. A transient initial model-start failure
  is retried even when the desired version has not changed.
- MLflow now saves exact source/configuration, SHA-256 manifest and resolved
  installed package versions alongside Git revision and frozen feedback.
- Added exporter scrape monitoring and a scrape-failure alert. The HTTP-5xx
  panel displays zero when the API is being scraped and no 5xx series exists.
- Added a runnable live acceptance checker, complete API/HLD/LLD documentation,
  explicit test cases, test results, and user instructions.

## Engineering coverage

| Guideline theme | Evidence and qualification |
|---|---|
| Problem and measurable outcomes | DESIGN.md scope and acceptance criteria |
| Data acquisition, bias and EDA | Bundled original BANKING77 splits/license/hashes; validation, class counts and text-length profile. English and closed-label bias documented. |
| Preprocessing and feature engineering | Ray transformations, Parquet, training-only TF-IDF unigrams/bigrams, no test vocabulary leakage, feature weights |
| Model development and comparison | Stratified validation, two candidates, selection by macro-F1, full refit and untouched official holdout |
| Automation, CI and versioning | Tested DVC graph, lock/cache, shared pipeline driver, quality gate and explicit run URIs |
| Reproducibility and environment parity | Git SHA plus exact source/feedback/parameter/environment snapshots. Train/test/serve use the same pinned Compose Python image. Preserve its build and local MLflow store for exact operational replay. |
| Deployment and rollback | Invocation-verified deployment, failed-load fallback, previous-version rollback and cache-reuse protection |
| Monitoring and maintenance | Input-distribution heuristics, reviewed agreement for the active model, conditional scheduled/drift retraining with cooldown, alerts and retention limits |
| Testing, logging and documentation | Isolated tests, real HTTP/lifecycle checks, stage/service logs, purpose-specific inline comments and project documentation |
| Security and encryption | Loopback binding, CORS, SQL binding, validation, safe UI output, timeouts and encrypted offline-backup instructions. Runtime HTTP/DB encryption, user authentication and production security are outside this local synthetic-data demo. Backup procedure is documented, not included in the executed tests. |
| Scalability | Resource limits and Ray batch transformations are implemented. Final preparation uses in-memory frames and SQLite is single-writer; large-scale throughput is not claimed. |
| No cloud platforms | Entire application stack is local Docker Compose. Initial public package/image downloads are the only installation dependency. |

## Test plan and acceptance criteria

Run isolated tests against temporary SQLite databases and mocked inference
boundaries; then run the actual trained MLflow service and API; finally execute
the complete Docker/Ray/Prometheus/Grafana checks on the target ARM64 VM. Keep
public test data out of training and use only synthetic support tickets.

| Test layer | Acceptance criterion | Reproduce |
|---|---|---|
| Unit/API/pipeline | All 58 cases pass, zero failures/errors; no state changes after invalid actions | `docker compose run --rm --no-deps trainer python -m pytest tests/ -q --junitxml=runtime/test-results.xml` |
| Tested training graph | Tests pass before fit; Ray reports 9,993 clean train and 3,080 official test rows; validation selection logged; test macro-F1 >= 0.65 | `python3 cli.py check` |
| MLproject and monitoring | MLproject completes; DVC DAG is generated; config/rule tests pass; model ready; all five `probe_success` values are 1 | `python3 cli.py verify` |
| Live application | Exact balances, idempotent retries, auditable reversals, live model provenance, saved human review and valid API contract | `python3 verify.py` |
| Model lifecycle and alerts | New reviewed feedback triggers training; rejected gate leaves serving version unchanged; rollback survives cached approval; model outage saves no ticket; alert fires and recovers | `python3 verify.py --lifecycle` |
| UI presentation | Three screens readable at 1366px and 390px/320px widths; forms/actions usable; no page-wide overflow; helpful error messages; no console errors | Browser inspection on the VM, including Ctrl+Shift+M responsive mode |
| Resource use | Record trainer samples and service snapshots; no OOM/restart; gates complete under the configured 3 GiB trainer limit | `cli.py check` and `cli.py verify` outputs |
| Persistence | Ledger, tickets and reviews survive backend restart; MLflow history survives normal stop/start | Record API/UI state, `docker compose restart backend`, then compare |

Performance values are measurements, not universal latency/throughput guarantees.
Docker's periodic memory sampling can miss short peaks; it is not an exact peak
resident-memory profiler. There is no separate high-concurrency load-test claim.

## Historical ZIP audit

These results apply to `BankFlow_Clear_Structure.zip`, audited on 12 November
2026 before the Ubuntu deployment. The [archive audit](evidence/audit.json)
retains the ZIP's file manifest and source hashes.

- Python tests: **58 passed, 0 failed, 0 errors, 0 skipped**.
  [Pytest report](evidence/test-results.xml). The test run used
  native Python 3.12.14; the Dockerfile specifies Python 3.11 for the VM.
- All 15 Python application/test files pass Black's 88-column formatting check.
- Bundled dataset bytes are unchanged from the original release archive, and
  its CSV checksums match provenance. Compose/external YAML, config references,
  dashboard JSON and DVC dependency paths were parsed and inspected.
- Native MLproject completed the tested DVC graph and actual model fitting,
  selection and held-out evaluation. Baseline logistic accuracy was
  **0.88474026**, macro-F1 **0.88509327**, on 3,080 rows.
- A live HTTP acceptance run exercised deposits, withdrawals, purchases,
  repayments, same-key retries, reversals, invalid input, actual MLflow inference
  and a saved review. Opening banking balances were restored.
- A DOM execution harness ran the release frontend's own JavaScript against
  live APIs: overview, deposit, reversal, classification, saved review, Operations fields, pipeline launch and cache-reuse display passed. This is
  a functional DOM check, not a screenshot/mobile-rendering result.
- Native lifecycle verification observed input drift, automatic reviewed-feedback
  retraining, a genuinely served new run ID, a rejected gate retaining the active
  model, rollback, cached-approval protection and fallback for an unloadable URI.
  The synthetic-feedback run used six eligible reviews, with held-out accuracy
  **0.88603896** and macro-F1 **0.88639926**. This single synthetic experiment is
  not evidence of a general real-world performance gain.

For the native MLproject/lifecycle checks, a disposable copy used pandas in the
`data_pipeline.prepare()` body because the workspace does not permit Ray's Unix socket. All
other application/model/pipeline logic was exercised directly. **The ZIP retains
real Ray Data and has no pandas fallback.** The original and adjusted source
hashes and check scope are recorded in `evidence/audit.json`.

## Isolated test cases

The rows below name the executable cases. Parametrized values are separate cases
in JUnit, giving the total of 58. The `test_real_api_model_boundary` case uses a
mock HTTP response; real MLflow inference is checked separately by `verify.py`.

| Executable test | Cases | Expected result |
|---|---:|---|
| `test_opening_balance_is_zero` | 1 | New account has zero savings and the full credit limit. |
| `test_deposit_withdrawal` | 1 | A deposit and withdrawal produce the exact balance. |
| `test_failed_withdrawal_preserves_state` | 1 | An overdraft is rejected without adding a ledger row. |
| `test_invalid_amount` | 5 | Zero, negative, fractional, boolean and excessive amounts are rejected. |
| `test_retry_is_idempotent` | 1 | Same key and payload return the same ID without a second deposit. |
| `test_changed_retry_rejected` | 1 | A changed payload cannot reuse a committed request key. |
| `test_reversal_keeps_original_and_only_once` | 1 | A compensating entry preserves the original and can occur only once. |
| `test_deposit_reversal_after_spending_rejected` | 1 | Reversing spent funds cannot overdraw savings. |
| `test_card_purchase_and_repayment` | 1 | Card debt and savings change by exact integer amounts. |
| `test_card_overpayment_and_limit_rejected` | 1 | Card overpayment and limit violations preserve state. |
| `test_card_repayment_reversal_restores_both` | 1 | Reversal restores savings and card debt. |
| `test_concurrent_duplicate_deposits` | 1 | Concurrent same-key requests create exactly one deposit. |
| `test_concurrent_withdrawals_cannot_overdraw` | 1 | Concurrent withdrawals cannot exceed the available balance. |
| `test_api_flow` | 1 | HTTP transaction, retry, account query and reversal work together. |
| `test_api_strict_paise` | 5 | HTTP rejects booleans, numeric strings, fractions, zero and negatives. |
| `test_unknown_reverse` | 1 | Unknown original ID returns 404. |
| `test_blank_support_query` | 1 | Whitespace-only text returns 422. |
| `test_real_api_model_boundary` | 1 | Mocked inference crosses the expected HTTP contract and saves/reviews a ticket. |
| `test_model_failure_does_not_save_ticket` | 1 | HTTP inference failure returns 503 and leaves the inbox empty. |
| `test_invalid_model_output_does_not_save_ticket` | 7 | Invalid scores, flag types, IDs, queues and labels return 503 without a ticket. |
| `test_queue_routing` | 8 | Eight representative labels reach the appropriate queue, including prior substring errors. |
| `test_review_keeps_original_prediction_and_exposes_corrected_route` | 1 | Original prediction persists while the review exposes its new queue. |
| `test_api_has_named_response_contracts` | 1 | Core endpoints expose nonempty response schemas in OpenAPI. |
| `test_pipeline_test_failure_cannot_promote_model` | 1 | A failing test stage stops the pipeline before prepare/train/deployment. |
| `test_pipeline_disabled_and_metrics` | 1 | Disabled launches return 403 and the metrics endpoint responds. |
| `test_test_duplicates_are_preserved` | 1 | Official holdout duplicates remain intact. |
| `test_training_deduplication_and_overlap_removal` | 1 | Only training duplicates and training/test overlaps are removed. |
| `test_conflicting_training_labels_are_rejected` | 1 | Conflicting normalized training labels are rejected. |
| `test_feedback_snapshot_excludes_reference_texts_and_conflicting_labels` | 1 | Public examples, inconsistent labels and unknown labels cannot contaminate feedback. |
| `test_drift_requires_samples_and_agreement_uses_active_version` | 1 | Drift respects sample readiness; reviewed agreement uses only the active model. |
| `test_retrain_policy` | 6 | Six combinations verify sample minimum, unchanged feedback, cooldown, drift and schedule. |
| `test_rollback_requires_real_previous_version_and_survives_cache_reuse` | 1 | Rollback requires a healthy predecessor and survives cached reapproval. |

## References

- [BANKING77 source and license](https://github.com/PolyAI-LDN/task-specific-datasets/tree/master/banking_data)
- [DVC pipeline definitions](https://doc.dvc.org/user-guide/pipelines/defining-pipelines)
- [DVC reproduction and dependencies](https://doc.dvc.org/command-reference/repro)
- [MLflow Projects and local environment execution](https://mlflow.org/docs/latest/projects/)
- [Docker Compose configurations](https://docs.docker.com/reference/compose-file/configs/)
