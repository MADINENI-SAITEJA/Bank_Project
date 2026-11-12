"""FastAPI routes, request metrics and background monitoring for BankFlow."""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

from pipeline import is_running, launch_pipeline, monitor_once
from data_pipeline import DATASET, dataset_categories, read_json
from routing import queue_for
from ledger import Ledger, RuleError
from schemas import (
    Transaction,
    Reverse,
    Ticket,
    Correction,
    Health,
    Balances,
    TransactionResult,
    LedgerEntry,
    Account,
    Prediction,
    TicketResult,
    StoredTicket,
    CorrectionResult,
    PipelineResult,
    PipelineRequestResult,
)


ROOT = Path(os.getenv("PROJECT_ROOT", ".")).resolve()
ledger = Ledger(os.getenv("BANKFLOW_DB", str(ROOT / "runtime/bankflow.sqlite3")))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bankflow")


async def monitor_loop():
    # Run local SQLite and filesystem work outside the API event loop.
    while True:
        try:
            await asyncio.to_thread(
                monitor_once,
                ROOT,
                ledger,
                os.getenv("ALLOW_PIPELINE_RUN", "0") == "1",
            )
        except Exception:
            log.exception("Monitoring update failed")
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(_app):
    if not (ROOT / "params.yaml").is_file():
        yield
        return
    task = asyncio.create_task(monitor_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="BankFlow",
    version="2.0.0",
    lifespan=lifespan,
    description="Local banking simulator and AI support routing.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("FRONTEND_ORIGINS", "http://localhost:8080").split(","),
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type"],
)
REQUESTS = Counter(
    "bankflow_http_requests_total", "Completed requests", ["method", "route", "status"]
)
LATENCY = Histogram("bankflow_http_seconds", "API latency", ["route"])
TRANSACTIONS = Counter("bankflow_transactions_total", "New ledger entries", ["kind"])
PREDICTIONS = Counter("bankflow_predictions_total", "Support predictions", ["queue"])
CONFIDENCE = Histogram(
    "bankflow_prediction_confidence",
    "Model probability, not calibrated certainty",
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1],
)
MODEL_LATENCY = Histogram("bankflow_model_seconds", "MLflow roundtrip duration")
RUNNING = Gauge("bankflow_pipeline_running", "Pipeline lock is held")
DRIFT = Gauge(
    "bankflow_input_drift", "Drift flag for a sufficiently large ticket window"
)
DRIFT_SAMPLES = Gauge("bankflow_drift_samples", "Accepted tickets in the drift window")
DRIFT_READY = Gauge(
    "bankflow_drift_ready", "Reference profile and minimum samples available"
)
DRIFT_JSD = Gauge(
    "bankflow_input_length_jsd", "Jensen-Shannon divergence of query lengths"
)
DRIFT_UNKNOWN = Gauge(
    "bankflow_input_unknown_delta", "Change in unfamiliar token share"
)
REVIEW_AGREEMENT = Gauge(
    "bankflow_reviewed_agreement", "Agreement on reviewed tickets for active model"
)
REVIEW_SAMPLES = Gauge(
    "bankflow_reviewed_samples", "Reviewed tickets for the active model"
)
MONITOR_TIME = Gauge(
    "bankflow_monitor_updated_timestamp_seconds", "Last input monitoring update"
)


@app.middleware("http")
async def instrument(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        REQUESTS.labels(request.method, route, "500").inc()
        LATENCY.labels(route).observe(time.perf_counter() - started)
        log.exception("Unexpected API failure")
        raise
    route = getattr(request.scope.get("route"), "path", "unmatched")
    if route != "/metrics":
        REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        LATENCY.labels(route).observe(time.perf_counter() - started)
    return response


@app.get("/health", response_model=Health)
def health():
    return {"status": "ok", "mode": "simulator"}


@app.get("/api/account", response_model=Account)
def account():
    return ledger.snapshot()


@app.post("/api/transactions", response_model=TransactionResult)
def transact(body: Transaction):
    try:
        result = ledger.transact(
            body.kind, body.amount_paise, body.request_key, body.memo
        )
    except RuleError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not result["replayed"]:
        TRANSACTIONS.labels(body.kind).inc()
    log.info(
        "transaction kind=%s id=%s replayed=%s",
        body.kind,
        result["transaction_id"],
        result["replayed"],
    )
    return result


@app.post(
    "/api/transactions/{transaction_id}/reverse", response_model=TransactionResult
)
def reverse(transaction_id: int, body: Reverse):
    with ledger.connect() as db:
        original = db.execute(
            "SELECT amount_paise FROM transactions WHERE id=?", (transaction_id,)
        ).fetchone()
    if not original:
        raise HTTPException(404, "Transaction not found.")
    try:
        result = ledger.transact(
            "reversal",
            original["amount_paise"],
            body.request_key,
            "Reversal",
            transaction_id,
        )
    except RuleError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not result["replayed"]:
        TRANSACTIONS.labels("reversal").inc()
    return result


@app.get("/api/categories", response_model=list[str])
def categories():
    path = ROOT / DATASET
    return dataset_categories(path) if path.is_file() else []


@app.post("/api/tickets", response_model=TicketResult)
async def ticket(body: Ticket):
    text = body.text.strip()
    if len(text) < 3:
        raise HTTPException(
            422, "Enter a support query with at least three characters."
        )
    started = time.perf_counter()
    try:
        # Model traffic stays on the local Compose network, independent of host proxies.
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(
                os.getenv("MODEL_URL", "http://localhost:5001") + "/invocations",
                json={"dataframe_records": [{"text": text}]},
            )
            response.raise_for_status()
            prediction = Prediction.model_validate(
                response.json()["predictions"][0]
            ).model_dump()
        if prediction["category"] not in categories() or prediction[
            "queue"
        ] != queue_for(prediction["category"]):
            raise ValueError("Invalid model output")
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        log.warning("Model unavailable: %s", type(exc).__name__)
        raise HTTPException(
            503, "Support model unavailable. Train and start the model service."
        ) from exc
    finally:
        MODEL_LATENCY.observe(time.perf_counter() - started)
    uri = "runs:/" + prediction["model_run_id"] + "/support_model"
    ticket_id = ledger.add_ticket(text, prediction, uri)
    PREDICTIONS.labels(prediction["queue"]).inc()
    CONFIDENCE.observe(prediction["confidence"])
    return {"id": ticket_id, **prediction}


@app.get("/api/tickets", response_model=list[StoredTicket])
def tickets():
    return ledger.tickets()


@app.patch("/api/tickets/{ticket_id}", response_model=CorrectionResult)
def correction(ticket_id: int, body: Correction):
    if body.category not in categories():
        raise HTTPException(422, "Choose a valid BANKING77 category.")
    try:
        ledger.correct(ticket_id, body.category)
    except RuleError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"status": "corrected"}


@app.get("/api/pipeline", response_model=PipelineResult)
def pipeline():
    running = is_running(ROOT)
    RUNNING.set(int(running))
    runs = []
    for path in sorted((ROOT / "runtime/runs").glob("*.json"), reverse=True)[:10]:
        try:
            runs.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    result = {
        "running": running,
        "runs": runs,
        "stages": ["test", "prepare", "train", "evaluate"],
        "metrics": None,
    }
    result["metrics"] = read_json(ROOT / "artifacts/metrics.json")
    result["preparation"] = read_json(ROOT / "data/prepared/prep_metrics.json")
    result["monitoring"] = read_json(ROOT / "runtime/monitoring.json")
    result["serving"] = read_json(ROOT / "runtime/serving.json")
    result["retraining"] = read_json(ROOT / "runtime/retraining.json")
    return result


@app.post("/api/pipeline/run", status_code=202, response_model=PipelineRequestResult)
def run_pipeline():
    if os.getenv("ALLOW_PIPELINE_RUN", "0") != "1":
        raise HTTPException(
            403, "Start the pipeline with the documented terminal command."
        )
    if is_running(ROOT):
        raise HTTPException(409, "A pipeline run is already active.")
    # The subprocess acquires the shared lock, including for simultaneous launch requests.
    launch_pipeline(ROOT)
    return {
        "status": "requested",
        "note": "Refresh to confirm start; existing run lock prevents overlap.",
    }


@app.get("/metrics")
def metrics():
    RUNNING.set(int(is_running(ROOT)))
    report = read_json(ROOT / "runtime/monitoring.json", {})
    DRIFT.set(int(report.get("drift", False)))
    DRIFT_SAMPLES.set(report.get("samples", 0))
    DRIFT_READY.set(int(report.get("ready", False)))
    REVIEW_SAMPLES.set(report.get("reviewed_samples", 0))
    MONITOR_TIME.set(report.get("updated_at", 0))
    # An unavailable estimate is NaN so Grafana does not display a misleading zero.
    for gauge, field in [
        (DRIFT_JSD, "length_jsd"),
        (DRIFT_UNKNOWN, "unknown_delta"),
        (REVIEW_AGREEMENT, "reviewed_agreement"),
    ]:
        value = report.get(field)
        gauge.set(float("nan") if value is None else value)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
