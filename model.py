"""MLflow prediction and the service that loads approved model runs."""

import json
import logging
import os
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path

import joblib
import pandas as pd
from mlflow.pyfunc import PythonModel
from routing import queue_for

log = logging.getLogger("bankflow.serving")


class SupportModel(PythonModel):
    """Keep serving independent of the API, with the MLflow run ID in each result."""

    def load_context(self, context):
        self.bundle = joblib.load(context.artifacts["bundle"])

    def predict(self, context, model_input, params=None):
        text = model_input["text"].fillna("").astype(str)
        vector = self.bundle["vectorizer"].transform(text)
        probabilities = self.bundle["model"].predict_proba(vector)
        classes = self.bundle["model"].classes_
        rows = []
        for p in probabilities:
            order = p.argsort()[::-1][:3]
            category = str(classes[order[0]])
            confidence = float(p[order[0]])
            rows.append(
                {
                    "category": category,
                    "confidence": confidence,
                    "model_run_id": self.bundle.get("run_id", "unknown"),
                    "queue": queue_for(category),
                    "needs_review": confidence < self.bundle["threshold"],
                    "alternatives": json.dumps(
                        [
                            {"category": str(classes[i]), "score": float(p[i])}
                            for i in order
                        ]
                    ),
                }
            )
        return pd.DataFrame(rows)


def stop(process):
    if process is not None and process.poll() is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def start_checked(uri):
    import httpx

    from pipeline import MODEL_URI

    if not MODEL_URI.fullmatch(uri):
        raise ValueError("Invalid model URI.")
    port = int(os.getenv("BANKFLOW_MODEL_PORT", "5001"))
    process = subprocess.Popen(
        [
            "mlflow",
            "models",
            "serve",
            "-m",
            uri,
            "--env-manager",
            "local",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--workers",
            "1",
        ],
        start_new_session=True,
    )
    try:
        with httpx.Client(timeout=2, trust_env=False) as client:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    response = client.post(
                        f"http://127.0.0.1:{port}/invocations",
                        json={
                            "dataframe_records": [
                                {"text": "My card payment was declined."}
                            ]
                        },
                    )
                    if response.is_success:
                        predicted = response.json()["predictions"][0]["model_run_id"]
                        if uri == f"runs:/{predicted}/support_model":
                            return process
                except (httpx.HTTPError, ValueError, KeyError, IndexError):
                    pass
                time.sleep(0.5)
        raise RuntimeError(
            "MLflow did not serve the requested version within 40 seconds."
        )
    except BaseException:
        stop(process)
        raise


def supervise():
    """Load requested versions, verify a prediction, and restore a healthy fallback."""
    from data_pipeline import read_json, write_json

    logging.basicConfig(level=logging.INFO)
    root = Path.cwd()
    status_path = root / "runtime/serving.json"
    saved = read_json(status_path, {})
    active = saved.get("active_uri")
    previous = saved.get("previous_uri")
    requested = None
    process = None

    def shutdown(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        while True:
            desired = read_json(root / "runtime/deployment.json", {}).get("desired_uri")
            if not desired or desired == requested:
                if desired and process is None:
                    # Retry an initial/transient startup failure even if the pointer
                    # has not changed. A healthy fallback is left running.
                    time.sleep(5)
                    requested = None
                if process is not None and process.poll() is not None:
                    log.error("Model process exited. Retrying after a short delay.")
                    requested = None
                    time.sleep(3)
                time.sleep(1)
                continue
            requested = desired
            state = {
                "requested_uri": requested,
                "active_uri": active,
                "previous_uri": previous,
            }
            write_json(status_path, {**state, "status": "loading"})
            stop(process)
            process = None
            try:
                process = start_checked(desired)
            except (RuntimeError, ValueError, OSError) as exc:
                log.error("Candidate failed to start: %s", exc)
                if active and active != desired:
                    try:
                        process = start_checked(active)
                    except (RuntimeError, ValueError, OSError):
                        log.exception("Previous model could not be restored.")
                write_json(
                    status_path,
                    {
                        **state,
                        "status": "fallback" if process else "failed",
                        "error": str(exc),
                    },
                )
            else:
                if active and active != desired:
                    previous = active
                active = desired
                write_json(
                    status_path,
                    {
                        "requested_uri": requested,
                        "active_uri": active,
                        "previous_uri": previous,
                        "status": "ready",
                    },
                )
                log.info("Serving %s", active)
    finally:
        stop(process)
        saved = read_json(status_path, {})
        write_json(status_path, {**saved, "status": "stopped"})


if __name__ == "__main__":
    supervise()
