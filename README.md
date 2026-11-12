# BankFlow

**Banking Transaction Simulator with AI Support Ticket Routing and AIOps**

BankFlow provides a local demo account with deposits, withdrawals, card purchases,
repayments and auditable reversals. Its English support inbox classifies queries
into 77 BANKING77 intents, suggests a support queue and records human reviews.
Operations connects the tested ML pipeline, deployment status and monitoring.
All money and support tickets are simulated. No requests go to a bank.

- [Design and API specification](DESIGN.md) contains the architecture, HLD and LLD.
- [Validation and rubric mapping](VALIDATION.md) contains the test plan, cases,
  results, acceptance criteria, recording checklist and viva preparation.
- [Attached test results](evidence/test-results.xml) and
  [audit evidence](evidence/audit.json) record the checks on this release.

## Start on your Ubuntu VM

Your earlier ARM64 Ubuntu VM had approximately 11 GiB RAM and Docker Compose.
Use the VM's terminal and browser. The software runs locally, with no cloud
platform. An internet connection is needed for the first Docker build and pulls.

1. Put `BankFlow_Clear_Structure.zip` in the VM's `~/Downloads` directory. Stop any
   older BankFlow stack from **its own project directory** to free ports 8000,
   8080, 5000, 9090 and 3000:

   ```bash
   docker compose --profile ai --profile monitoring stop
   ```

2. Extract into a new directory so your earlier project and its data stay intact:

   ```bash
   mkdir -p "$HOME/bankflow_clear_structure"
   python3 -m zipfile -e "$HOME/Downloads/BankFlow_Clear_Structure.zip" "$HOME/bankflow_clear_structure"
   cd "$HOME/bankflow_clear_structure/BankFlow"
   git init -b main
   mkdir -p .dvc runtime artifacts mlartifacts
   touch .dvc/config
   git add .
   git commit -m "Add checked BankFlow project"
   ```

   If Git asks for an identity, set `git config user.name "Your Name"` and
   `git config user.email "your-git-email@example.com"` in this checkout, then
   retry the commit. The commit is required for experiment provenance.

3. Build and run the test/training pipeline:

   ```bash
   docker compose config -q
   docker compose build backend frontend
   python3 cli.py check
   ```

   This runs **test → prepare → train → evaluate**, records stage logs and
   samples trainer resources. Preparation uses real Ray Data. MLflow compares
   Naive Bayes with logistic regression and logs the chosen model. The quality
   gate requires held-out macro-F1 of at least 0.65. If a stage fails, read its
   named log in `runtime/runs/`; the previous deployment is retained.

4. Record DVC's lock and start all seven long-running services:

   ```bash
   git add dvc.lock
   git diff --cached --quiet || git commit -m "Record successful BankFlow pipeline"
   docker compose --profile ai --profile monitoring up -d \
     backend frontend model mlflow prometheus grafana probe
   python3 cli.py verify
   python3 verify.py
   ```

   `cli.py verify` checks the MLproject entry point, DVC DAG, Prometheus
   configuration and alert-rule tests, approved model and five service probes.
   `verify.py` exercises live banking and support APIs. It adds eight audit
   entries, restores the opening balances, and keeps one synthetic reviewed
   ticket. Reports go to `runtime/verification/`.

The configurations are ordinary files under `frontend/` and `monitoring/`.
Use Docker Compose v2; your previously reported Compose 2.40.3 supports this layout. Docker and genuine Ray execution
of this corrected release must be confirmed on the VM; see the validation report.

| Open in the VM browser | Purpose |
|---|---|
| http://localhost:8080 | BankFlow application |
| http://localhost:8000/docs | Interactive request and response contracts |
| http://localhost:5000 | MLflow experiments and model artifacts |
| http://localhost:9090/targets | Prometheus scrape and probe status |
| http://localhost:3000/d/bankflow/bankflow-operations | Grafana dashboard |

Grafana demo login is `admin` / `bankflow-local-demo`. Set `GRAFANA_PASSWORD`
in a local `.env` before the first launch to choose a different password.

## Where to find things

| Area | Files | Purpose |
|---|---:|---|
| Python modules at the project root | 10 | API, ledger, schemas, routing, data preparation, training, orchestration, model serving, VM commands and acceptance checks |
| `frontend/` | 4 | HTML, CSS, JavaScript and Nginx configuration |
| `tests/` | 5 | Shared fixtures plus ledger, API, data and pipeline tests |
| `monitoring/` | 7 | Prometheus, alerts, probe settings and Grafana provisioning/dashboard |
| Data, build settings, documentation and evidence | 14 | Reproducible dataset, Docker/DVC/MLproject configuration, guides and test reports |

Every file is listed with its role in [DESIGN.md](DESIGN.md). Run the commands
from the project root. The documented host commands use `cli.py`; existing
`python -m pipeline` commands remain supported.

## User manual

### Account overview

Select an operation, enter a positive amount with at most two decimal places,
add an optional description and select **Confirm transaction**. Savings begin
at ₹0 and the simulated credit limit is ₹50,000.

- A **deposit** increases savings. A **withdrawal** reduces savings.
- A **card purchase** increases card debt. **Pay card from savings** reduces
  savings and card debt by the same amount.
- Select **Reverse** beside a transaction and confirm. The original remains in
  history with a linked reversal entry. Reversals that would overdraw savings
  or violate the card limits are rejected.
- A network error may leave an uncertain transaction result. Retry with the
  same inputs in the same page session to reuse its request key. Refreshing
  the entire browser page clears the in-memory retry key; check history first.

For a fresh account, deposit ₹1,000, withdraw ₹200, make a ₹300 card purchase,
repay ₹100, and reverse the repayment. Savings should be ₹800 and card debt
₹300, with five entries. The eight acceptance-test entries may already exist;
compare the new entries rather than expecting the total to be five.

### Support inbox

Enter a short banking query in English, such as “My card payment was declined.”
Select **Classify request**. The result contains an intent, queue and model
score. A score below 45% is marked **Needs review**. Scores are uncalibrated
model probabilities and can be wrong even when high.

Choose the correct category in the ticket's dropdown and select **Save
correction**. The displayed queue follows the reviewed category. The original
prediction and its score remain visible for audit. The score does not become
a confidence estimate for the new label. One review does not immediately
retrain the model. Eligible reviewed feedback is used by the monitored pipeline.

If the model is unavailable, an error is shown and no ticket is saved. Wait
for **Live model: ready** in Operations before retrying. Version reloads may
briefly interrupt classification. Use fictional queries without personal data.

### Operations and monitoring

The four stage cards explain the pipeline. **Run pipeline** requests a tested
DVC run. The run list shows passed/failed stages, elapsed times and whether
outputs were reused. **Data preparation** shows row counts and throughput.
**Live model** distinguishes the requested version from the version actually
serving. **Input monitoring** reports drift readiness and reviewed agreement.

Use **Open MLflow** to select **BankFlow support routing**. Expand the plus
beside **validation-selection** to see `naive_bayes` and `logistic`. The parent
Artifacts tab contains `support_model`, `evaluation`, `data`, `explainability`
and `provenance`. **BankFlow Pipeline Runs** contains MLproject driver runs.

Use **Open Grafana** and a recent time range, then Refresh. Allow two scrapes
(about 30 seconds) for new traffic to appear. Service-health value `1` indicates
a successful probe. A series color alone does not indicate failure. Model
latency needs inference traffic, and drift needs at least 50 recent tickets.
A Prometheus target marked UP means its scrape worked; inspect `probe_success`
to determine whether the probed application itself is healthy.

## Full lifecycle demo

After the normal checks, run this in the new demo checkout:

```bash
python3 verify.py --lifecycle
```

This adds synthetic reviewed traffic, triggers automatic retraining, waits
for a new deployed run, deliberately rejects a candidate with a gate of 1.0,
restores the original setting, rolls back, checks cache reuse, stops the model,
checks that a failed inference saves no ticket, waits for its Prometheus alert,
and restarts the model to check recovery. It takes several minutes because the
alert has a two-minute hold. Run it once in a fresh checkout; automatic retraining
has a one-hour cooldown. It intentionally changes demo tickets and model history.
The gate setting and stopped model are restored through `finally` blocks.

For a short manual rollback after two healthy versions exist:

```bash
docker compose run --rm --no-deps trainer python -m pipeline rollback
```

## Reproduction and backups

A model run records Git SHA, exact source/configuration ZIP with SHA-256 hashes,
installed package versions, parameters, raw dataset hashes and frozen feedback.
To replay, use the recorded Git revision, overlay that run's `provenance/source.zip`
if needed, and restore its `params.yaml` and `data/reviewed_feedback.json` artifact
as `runtime/replay.json`. Then use the same Docker image/environment and run:

```bash
docker compose run --rm --no-deps trainer \
  python -m pipeline run --feedback-file runtime/replay.json
```

Compare input hashes, parameters and metrics. The replay receives a new run ID.
The source snapshot is necessary when the recorded working tree was dirty.
Preserve `runtime/`, `mlartifacts/`, `artifacts/`, `data/prepared/` and `.dvc/cache/`
for local history and rollback. GitHub contains the code and reproducible dataset,
not those generated stores. No DVC remote is required for this local assignment.

For an encrypted offline backup with GnuPG installed, stop the stack first:

```bash
docker compose --profile ai --profile monitoring stop
set -o pipefail
tar -czf - --exclude='.git' --exclude='.venv' --exclude='.pytest_cache' \
  --exclude='__pycache__' . | gpg --symmetric --cipher-algo AES256 \
  --output "$HOME/BankFlow-backup-$(date +%Y%m%d-%H%M%S).tar.gz.gpg"
```

GnuPG asks for a passphrase. Keep it separately; it cannot be recovered from the
backup. Restore into an empty directory using `gpg --decrypt backup.tar.gz.gpg |
tar -xzf -`. This protects the exported backup. Runtime databases and HTTP
traffic remain unencrypted in the local demo. Named Prometheus/Grafana volumes
are not in this archive; their configuration can be recreated from Compose.

## Submit and record

Push the extracted source files, the supplied evidence, `.dvc/config` and your
VM-generated `dvc.lock`. For fresh VM evidence, create `evidence/vm/` and copy
`runtime/test-results.xml`, `runtime/verification/`, `artifacts/metrics.json`,
`artifacts/training.json` and `data/prepared/prep_metrics.json` into it. Inspect
these before committing. They should contain only synthetic demo data.

The project contains 40 packaged files, organized by responsibility. Each has a defined role in the file guide
in [DESIGN.md](DESIGN.md). Generated model binaries, databases and caches are
ignored by Git. Do not run `docker compose down -v` if you want to keep dashboard
history. Use `docker compose --profile ai --profile monitoring stop` to pause,
and the earlier `up -d` command to resume.

## AI assistance disclosure

OpenAI ChatGPT/Codex assisted with design, Python and frontend implementation,
Docker/DVC/MLflow configuration, tests, debugging, documentation and refactoring.
The evidence states what was actually checked and which VM checks remain.
The student must understand and demonstrate the project and follow their
course's disclosure rules.
