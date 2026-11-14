# BankFlow high-level design

## Problem, scope and success criteria

A customer should be able to understand simulated banking transactions, correct
mistakes through an auditable reversal, and describe a banking support problem
without choosing among 77 technical labels. An operator should be able to trace
model decisions to experiments, observe service failures and data shifts, and
replace or restore a model using a tested, reproducible local pipeline.

The agreed project scope is **Banking Transaction Simulator with AI Support
Ticket Routing and AIOps**. It is a single-account educational simulator, not a
bank integration, payment processor, chatbot or authenticated banking product.
Routing assigns a local queue; it does not deliver tickets to external staff.

Measurable acceptance criteria are exact integer balances with no overdrafts or
duplicate retries; preserved reversal history; recorded classification and
human review; no ticket saved on inference failure; tests before training;
held-out macro-F1 >= 0.65; traceable model runs; a quality-gated deployment with
fallback and rollback; real Ray preparation timings; and healthy monitored
services. See the [test plan and acceptance criteria](VALIDATION.md#test-plan-and-acceptance-criteria) for the verification steps.

## Architecture

```mermaid
flowchart TD
    U["Browser: Overview, Support, Operations"] -->|"configurable REST"| A["FastAPI backend"]
    A --> L["SQLite ledger and tickets"]
    A -->|"HTTP inference"| M["MLflow Serve and supervisor"]
    A -->|"locked child process"| P["DVC test and training pipeline"]
    P --> D["Ray Data and sklearn"]
    D --> S["MLflow SQLite store and artifacts"]
    M --> S
    T["MLflow tracking UI"] --> S
    P -->|"approved run pointer"| M
```

| Block | Responsibility and boundary |
|---|---|
| Nginx/browser | Separate frontend image; renders three screens and sends JSON to the configured API. It imports no Python/model code. |
| FastAPI | Validates requests, applies ledger rules, calls the independent model service and exposes operational state and Prometheus metrics. |
| SQLite application DB | Persists append-only movements and original predictions plus review labels. The application DB is separate from MLflow's DB. |
| Ray/sklearn/DVC | Validates public data, runs automated tests, prepares Parquet, compares classifiers and enforces the quality gate. |
| MLflow | Tracks parameters, metrics, nested candidates, source and environment snapshots, features, data profiles and model/evaluation artifacts. |
| Model supervisor | Starts the real MLflow REST server. It verifies an invocation's run ID before declaring a deployment ready. |
| Prometheus/Grafana/probe | Scrapes API/process metrics and exporter health, probes five applications, evaluates alerts and renders the provisioned dashboard. |

```mermaid
flowchart TD
    P["Prometheus"] -->|"scrape /metrics"| A["API and process metrics"]
    P -->|"probe requests"| B["Blackbox exporter"]
    B --> S["Frontend, API, model, MLflow, Grafana"]
    G["Grafana"] -->|"PromQL queries"| P
```

The Python services share a mounted project directory, including local MLflow
storage and model artifacts. The frontend talks only to the API. Metrics are
pulled by Prometheus. The short-lived trainer records resource snapshots during
`cli.py check`; it is not a permanently running health-probe target.

## High-level design and rationale

```mermaid
flowchart TD
    T["Automated tests"] --> F["Fit candidates and select on validation"]
    R["Ray preparation"] --> F
    F --> E["Official holdout evaluation"]
    E -->|"gate passes"| V["Request approved version"]
    V --> H["Verify serving run ID"]
    H -->|"load fails"| B["Restore healthy fallback"]
```

The DVC DAG has `test` and `prepare` feeding `train`, followed by `evaluate`.
The driver runs test, prepare, train and evaluate in that order for a clear
Operations history. `train` depends on the successful JUnit report, so a failing
test also blocks `dvc repro train` and `dvc repro evaluate`. UI launches and
monitor-triggered launches use the same driver. Deployment is requested only
after the entire driver succeeds. DVC skips unchanged stages and stores output
hashes in `dvc.lock`; the displayed "reused" status is intentional.

| Choice | Reason | Tradeoff |
|---|---|---|
| Static HTML/CSS/JavaScript | Small separate UI with no framework build chain | Separate HTML, CSS and JavaScript require no bundler; asset caching is basic |
| FastAPI and functional pipeline stages | Explicit REST contracts, validation, and independently testable computations | Operations still relies on shared local storage |
| `Ledger` and `SupportModel` classes | Encapsulate database invariants and MLflow's model lifecycle | This is a pragmatic mix of OO boundaries and functional transformations |
| Integer paise and SQLite transactions | Exact arithmetic and atomic retry/balance/insert checks | Single-writer throughput limits large deployments |
| TF-IDF + Naive Bayes/logistic regression | Fast CPU baselines, low memory and inspectable feature weights | English closed-set intent recognition; no semantic reasoning or calibrated certainty |
| Validation macro-F1 | Gives equal importance to intents despite unequal training counts | The official test set must not be used to tune hyperparameters |
| Local Docker Compose | Fits the ARM64 VM and the no-cloud requirement | No cluster, zero-downtime deployment or production high availability |
| MLflow run URI per model | Versions remain traceable without a remote registry | Preserve the local tracking DB and artifacts to keep rollback possible |
| Length/token drift plus reviewed agreement | Lightweight, explainable monitoring | A drift flag is a heuristic, not proof of accuracy loss |

Selection uses a stratified 80/20 split of cleaned training data, seed 42. The
TF-IDF vocabulary is fitted only on fitting rows; validation examples do not
build it. Two classifiers use the same features. The selected classifier is
refitted on all cleaned training rows plus eligible reviewed feedback. Only then
is it evaluated on the untouched official test split. The fixed test gate is a
release check; repeated checking is not an independent estimate for every new
real-world distribution.

## Data and experiments

`data/banking77.zip` contains the original CSV splits, 77 labels and
`PROVENANCE.json` with upstream attribution, CC BY 4.0 license and checksums.
Preparation verifies both CSV hashes, rejects missing/blank queries and
conflicting normalized labels, deduplicates training rows and removes six
train/test overlaps from training. It preserves all 3,080 official test rows.
The cleaned training set has 9,993 rows, with 35–187 examples per intent; each
test intent has 40 examples. EDA outputs include class counts, query lengths,
vocabulary and unknown-token fractions in `data/prepared/profile.json`.

Ray Data reads CSV and applies cleaning batches; results are collected to the
driver for dataset-level deduplication and Parquet export. This is appropriate
for BANKING77, not a claim of unbounded streaming scalability. Preparation
reports total wall time and cleaned rows per second, including Ray startup.
Model timings exclude data preparation; the Operations history gives total
stage command times including DVC overhead.

The model supports five local queues: Top-ups, Cash & ATM, Transfers, Cards and
Account support. Routing checks whole issue tokens and prioritizes top-up and
cash issues so, for example, `topping_up_by_card` is Top-ups and `card_swallowed`
is Cash & ATM. Human reviews can change the effective route without overwriting
the original prediction. Queue membership is an application rule, not a second
trained classifier.

MLflow stores the parent selection run, two child candidate runs, feature
weights, final model, data profile, feedback snapshot, confusion matrix and
per-class evaluation report. Tags include Git revision, dirty-tree status,
selected classifier and quality gate. `provenance/source.zip` and its hash
manifest preserve the exact executed public code/configuration. Package versions
and platform are in `provenance/environment.json`. A dirty commit alone is
insufficient to reproduce a run; use both the recorded revision and snapshots.
The MLproject driver is separate from the candidate parent run.

## File responsibilities

The repository includes application code, configuration, data, tests and documentation.
Setup and operation generate the local runtime stores.

| File | Responsibility |
|---|---|
| `app.py` | FastAPI routes, HTTP instrumentation and the background monitoring task |
| `ledger.py` | SQLite schema, transactional money rules, audit entries and ticket persistence |
| `schemas.py` | Pydantic request, response and inference validation contracts |
| `routing.py` | BANKING77 category-to-queue rules shared by inference and reviews |
| `data_pipeline.py` | Dataset hashes, Ray cleaning, reference profiles, drift calculations and feedback filtering |
| `training.py` | Candidate comparison, MLflow provenance, feature reports and held-out quality gate |
| `pipeline.py` | DVC stage execution, run lock, monitored retraining and deployment/rollback decisions |
| `model.py` | MLflow inference wrapper and invocation-verified serving supervisor |
| `cli.py` | Host/VM commands for training, verification, demo and rollback |
| `verify.py` | Live API acceptance and optional full VM lifecycle checks |
| `frontend/index.html` | Semantic markup for account, support and operations screens |
| `frontend/styles.css` | Visual styling and responsive layouts |
| `frontend/app.js` | Service URLs, forms, HTTP requests, state updates and escaped rendering |
| `frontend/nginx.conf` | Static frontend delivery, health route and response headers |
| `tests/conftest.py` | Temporary database and HTTP-client fixtures |
| `tests/test_ledger.py` | Balance, retry, concurrency, reversal and saved-review tests |
| `tests/test_api.py` | HTTP contract, validation, metrics and inference-error tests |
| `tests/test_training.py` | Leakage, preparation, feedback, queue and drift tests |
| `tests/test_pipeline.py` | Test-stage rejection, retraining policy and rollback tests |
| `monitoring/prometheus.yaml` | Scrape intervals, API/exporter targets and application probes |
| `monitoring/alerts.yaml` | Service, scrape, API error, drift and stale-monitor alert rules |
| `monitoring/alert_tests.yaml` | Promtool fixtures for service and drift alert firing/recovery |
| `monitoring/blackbox.yaml` | HTTP probe module and timeout |
| `monitoring/grafana_datasource.yaml` | Grafana connection to the local Prometheus service |
| `monitoring/grafana_provider.yaml` | Grafana dashboard provisioning location |
| `monitoring/grafana_dashboard.json` | Dashboard panels, units and PromQL queries |
| `Dockerfile` | Separate Nginx frontend and shared Python build targets |
| `requirements.txt` | Direct Python dependency version pins |
| `compose.yaml` | Service commands, resource limits, volumes, ports and configuration mounts |
| `params.yaml` | Preparation, classifier, quality-gate and monitoring parameters |
| `dvc.yaml` | Tested data/model dependency graph and generated outputs |
| `dvc.lock` | Recorded stage definitions, dependency hashes and output hashes |
| `.dvc/config` | Repository-level DVC configuration |
| `MLproject` | MLflow project entry point using the same Python image |
| `data/banking77.zip` | Original public dataset, labels, license attribution and hashes |
| `README.md` | Installation, user guide, verification, and reproduction |
| `HLD.md` | Scope, architecture, design rationale, data and experiments |
| `LLD.md` | API contracts, persistence, deployment and monitoring |
| `VALIDATION.md` | Test plan, acceptance criteria, verification results, and evidence |
| `evidence/test-results.xml` | Pytest report from the original archive audit |
| `evidence/audit.json` | Historical source hashes and validation results for the original ZIP |
| `evidence/vm/` | 19 Ubuntu reports covering tests, training, evaluation, monitoring, model lifecycle, resources, and source revision |
| `.gitignore` | Excludes generated stores, caches and local environment settings |
| `.dockerignore` | Excludes generated/local files from the Docker build context |

The root Python modules form one small application. Folders group static frontend
assets, tests and monitoring configuration; no framework scaffolding is required.
Business rules stay in the ledger, HTTP contracts in schemas, model fitting outside
the API, and host Docker commands outside the DVC orchestration logic.
