# BankFlow low-level design

See the [architecture](HLD.md#architecture) for component boundaries and the [high-level design](HLD.md) for the design rationale.

## Persistence and transaction invariants

`transactions` stores ID, unique request key, serialized request payload, kind,
amount, savings/card deltas, memo, optional unique `reverses` foreign key and UTC
timestamp. Balances are sums of deltas, never floating-point currency.
`BEGIN IMMEDIATE` serializes retry lookup, balance checks and insert. An identical
request key/payload replays the existing ID; a changed payload with the same key
returns 409. Savings must stay nonnegative and card debt must be in
[0, 5,000,000] paise. Reversals negate the original deltas once and cannot reverse
a reversal. Connections commit or roll back and are explicitly closed.

`tickets` stores text, original category/confidence/queue, review flag, corrected
category, source model URI and UTC timestamp. Effective corrected queue is derived
from the review label. Failed model calls or malformed predictions are rejected
before insertion. Review labels and original predictions support agreement and
feedback audits. Lists expose only the latest 100 entries; the DB retains history.

## REST contract

All API JSON uses UTF-8. Money is integer paise. There is no authentication in
this loopback-only demo. The frontend's `BANKFLOW_CONFIG` at the top of `frontend/app.js` controls
API, MLflow and Grafana URLs. Backend `MODEL_URL` controls the inference endpoint;
`FRONTEND_ORIGINS` controls browser CORS and `ALLOW_PIPELINE_RUN` enables launches.
The generated `/openapi.json` and `/docs` have named request/response schemas.

| Method and path | Input | Success output | Other statuses |
|---|---|---|---|
| GET `/health` | none | 200 `{status:"ok", mode:"simulator"}` | 500 for unexpected server failure |
| GET `/api/account` | none | 200 `Account` | 500 for unexpected storage failure |
| POST `/api/transactions` | `Transaction` | 200 `TransactionResult` | 409 rule conflict; 422 validation |
| POST `/api/transactions/{transaction_id}/reverse` | integer ID, `Reverse` | 200 `TransactionResult` | 404 missing original; 409 invalid reversal; 422 validation |
| GET `/api/categories` | none | 200 array of category strings | 500 corrupt dataset |
| POST `/api/tickets` | `Ticket` | 200 `TicketResult` | 422 invalid/blank text; 503 model timeout, failure or invalid output |
| GET `/api/tickets` | none | 200 array of `StoredTicket` | 500 storage failure |
| PATCH `/api/tickets/{ticket_id}` | integer ID, `{category: string}` | 200 `{status:"corrected"}` | 404 missing ticket; 422 unknown category |
| GET `/api/pipeline` | none | 200 `PipelineResult` | 500 unreadable/corrupt state |
| POST `/api/pipeline/run` | no body | 202 `{status:"requested", note:string}` | 403 launches disabled; 409 lock already held |
| GET `/metrics` | none | 200 Prometheus text | 500 unreadable/corrupt monitoring state |

Framework-handled errors use `{"detail": "message"}`; validation errors use a
`detail` list describing the fields. Unexpected server errors are logged without
returning tracebacks to the UI. Middleware counts them as HTTP 500 and records
latency. Support-model failures become an actionable 503 message.

| Schema | Fields and constraints |
|---|---|
| Transaction | `kind`: deposit/withdrawal/card_purchase/card_repayment; `amount_paise`: strict integer 1–100,000,000 (booleans/strings/floats rejected); `request_key`: 1–100 characters; `memo`: optional string, maximum 200 |
| Reverse | `request_key`: 1–100 characters |
| Balances | `balance_paise`, `card_due_paise`, `card_limit_paise`, `card_available_paise`: integers |
| TransactionResult | Balances plus `transaction_id`: integer and `replayed`: boolean |
| LedgerEntry | `id`, `amount_paise`, `savings_delta`, `card_delta`: integers; `kind`, `memo`, `created_at`: strings; `reverses`: integer or null; `reversed`: 0/1 |
| Account | Balances plus `transactions`: LedgerEntry array, newest first |
| Ticket | `text`: string 3–1,000 characters, trimmed, at least three after trimming |
| TicketResult | `id`: integer; `category`: BANKING77 label; `confidence`: finite number [0,1]; `queue`: one of five queue names; `needs_review`: boolean; `model_run_id`: 32 lowercase hex characters; `alternatives`: JSON-encoded string of up to three `{category, score}` objects |
| StoredTicket | `id`, `text`, original `category`, `confidence`, `queue`, `needs_review` (0/1), nullable `corrected_category` and `corrected_queue`, `model_uri`, `created_at` |
| PipelineResult | `running`: boolean; `stages`: array; `runs`: newest ten reports; nullable `metrics`, `preparation`, `monitoring`, `serving`, `retraining` objects as specified below |

The support API additionally checks that the model category is known, the queue
matches that category and the run ID is valid. Invalid scores/flags/queues return
503 without saving a ticket. MLflow's internal `/invocations` accepts
`{"dataframe_records":[{"text":"My card payment was declined."}]}` and returns
`{"predictions":[...]}`. Banking APIs never load the trained model directly.

`PipelineResult.runs` entries contain `id`, status, UTC start time, total seconds,
reviewed-training count and per-stage `name`, `status`, `seconds`, `mode`
(executed/reused); failures include `error`. Passed reports include `approved_uri`.
`metrics` contains test accuracy/macro-F1, row count, evaluation time, gate threshold,
gate result and per-class classification report. `preparation` contains row
counts, overlap removal, classes, seconds, rows/second and source hashes.
`serving` contains status (loading/ready/fallback/failed/stopped), requested URI,
active URI, previous URI and optional error. `monitoring` contains samples,
readiness, length JSD, unknown-token change, drift, active-model reviewed count
and agreement, and update time. `retraining` contains start/last-attempt times,
feedback digest, trigger reason and reviewed-row count.

## Retraining, deployment and failure handling

The monitor checks every 15 seconds. A maximum 200-ticket window needs 50 rows
before drift is assessed. JSD >= 0.12 or unknown-token increase >= 0.15 flags
drift. Retraining also needs at least five distinct eligible reviews, changed
feedback, a ready model, an idle pipeline and a one-hour cooldown. A daily check
can trigger training when new reviewed feedback exists even without drift.
It does not train repeatedly on unchanged labels or manufacture labels from
model guesses. Public split examples and contradictory reviews are excluded.

`runtime/pipeline.lock` uses a nonblocking advisory lock shared by training and
rollback. Status writes use atomic rename. A passing run updates a desired URI;
the supervisor loads it and checks a real returned run ID. Failure restores the
last active model when possible. An initial startup failure is retried. The
previous healthy URI enables rollback. Remembering the last approved candidate
prevents DVC cache reuse from silently undoing an operator's rollback. Reloading
uses a single model process and may create a short inference outage.

## Monitoring and security boundary

Metrics cover requests by method/route/status, API latency, transaction counts,
predictions by original queue, model round-trip latency/confidence, API CPU/memory,
pipeline lock, drift and active-model reviewed agreement. Five blackbox probes
check frontend, backend, model, MLflow and Grafana. Prometheus also scrapes itself
and the exporter. `up == 0` alerts catch exporter/scrape failures. Service failure
has a two-minute hold, API error rate a two-minute hold, and drift/stale monitoring
a 30-second hold. Alerts appear in Prometheus; email/Slack delivery is not configured.

Published ports bind to 127.0.0.1; model and exporter ports stay internal. Browser
CORS is restricted, SQL uses bound parameters, UI output is escaped, HTTP calls
have timeouts, monetary inputs are bounded and inference output is validated.
Only trusted locally trained joblib/pickle artifacts should be loaded. Nginx adds
nosniff and frame-denial headers. Runtime storage is not encrypted, and HTTP is
not TLS. README provides an encrypted offline-backup procedure. These controls
are scoped to synthetic data on a single-user local VM. Real banking deployment
would require authentication/authorization, TLS, encrypted storage, secrets and
key management, durable audit trails and further security testing.
