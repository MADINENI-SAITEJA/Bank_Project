"""DVC orchestration, gated deployment, rollback and monitored retraining."""

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

from data_pipeline import (
    drift_report,
    parameters,
    read_json,
    reviewed_feedback,
    write_json,
)

MODEL_URI = re.compile(r"runs:/[a-f0-9]{32}/support_model")


def reviewed_rows(path):
    with closing(sqlite3.connect(path, timeout=20)) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in db.execute(
                "SELECT text, corrected_category AS category FROM tickets "
                "WHERE corrected_category IS NOT NULL ORDER BY id DESC LIMIT 500"
            )
        ]


def promote(root, uri):
    if not MODEL_URI.fullmatch(uri):
        raise ValueError("Invalid MLflow model URI.")
    path = root / "runtime/deployment.json"
    current = read_json(path, {})
    # A cached evaluation of the same candidate must not undo an operator rollback.
    if current.get("last_approved_uri") != uri:
        write_json(path, {"desired_uri": uri, "last_approved_uri": uri})


def rollback(root):
    lock_path = root / "runtime/pipeline.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = root / "runtime/deployment.json"
        current = read_json(path, {})
        serving = read_json(root / "runtime/serving.json", {})
        if serving.get("status") not in ("ready", "fallback"):
            raise ValueError("Wait for the model service to finish loading.")
        if current.get("desired_uri") != serving.get("requested_uri"):
            raise ValueError("Wait for the current deployment request to finish.")
        if serving["status"] == "fallback":
            target = serving.get("active_uri")
        else:
            target = serving.get("previous_uri")
        if not target or not MODEL_URI.fullmatch(target):
            raise ValueError("A previous healthy model version is required.")
        current["desired_uri"] = target
        write_json(path, current)
    return target


def is_running(root):
    path = Path(root) / "runtime/pipeline.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def snapshot_feedback(root, replay=None):
    if replay is not None:
        return reviewed_feedback(root, read_json(replay)["rows"])
    path = Path(os.getenv("BANKFLOW_DB", str(root / "runtime/bankflow.sqlite3")))
    if path.is_file():
        return reviewed_feedback(root, reviewed_rows(path))
    return []


def launch_pipeline(root):
    log_path = root / "runtime/launcher.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "pipeline", "run"],
            cwd=root,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
        )
    threading.Thread(target=process.wait, daemon=True).start()
    return process


def measure(root, ledger):
    cfg = parameters(root)["monitor"]
    profile = read_json(root / "data/prepared/profile.json")
    serving = read_json(root / "runtime/serving.json", {})
    report = drift_report(
        profile,
        ledger.recent_monitoring_rows(cfg["window"]),
        serving.get("active_uri"),
        cfg,
    )
    report["updated_at"] = time.time()
    write_json(root / "runtime/monitoring.json", report)
    return cfg, report


def retrain_reason(cfg, report, state, count, digest, now):
    if count < cfg["minimum_reviewed"] or digest == state.get("last_attempt_digest"):
        return None
    if now - state.get("last_attempt_at", 0) < cfg["cooldown_seconds"]:
        return None
    if report["drift"]:
        return "input drift"
    if (
        now - max(state["started_at"], state.get("last_attempt_at", 0))
        >= cfg["scheduled_interval_seconds"]
    ):
        return "scheduled review"
    return None


def monitor_once(root, ledger, auto_enabled):
    cfg, report = measure(root, ledger)
    if not auto_enabled or not cfg["automatic_retraining"] or is_running(root):
        return None
    serving = read_json(root / "runtime/serving.json", {})
    if serving.get("status") != "ready":
        return None
    path = root / "runtime/retraining.json"
    now = time.time()
    state = read_json(path)
    if state is None:
        state = {"started_at": now}
        write_json(path, state)
    feedback = reviewed_feedback(root, reviewed_rows(ledger.path))
    digest = hashlib.sha256(json.dumps(feedback, sort_keys=True).encode()).hexdigest()
    current = read_json(root / "runtime/reviewed_feedback.json", {"rows": []})
    if feedback == current["rows"]:
        return None
    reason = retrain_reason(cfg, report, state, len(feedback), digest, now)
    if reason is None:
        return None
    process = launch_pipeline(root)
    write_json(
        path,
        {
            **state,
            "last_attempt_at": now,
            "last_attempt_digest": digest,
            "last_reason": reason,
            "reviewed_rows": len(feedback),
        },
    )
    return process


def run_pipeline(feedback_file=None):
    """Run the tested DVC graph under one lock, then request a gated deployment."""
    root = Path.cwd()
    runs = root / "runtime/runs"
    runs.mkdir(parents=True, exist_ok=True)
    with (root / "runtime/pipeline.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("A pipeline run is already active.") from exc
        run_id = str(time.time_ns())
        path = runs / f"{run_id}.json"
        report = {
            "id": run_id,
            "status": "running",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stages": [],
        }
        write_json(path, report)
        all_start = time.perf_counter()
        # MLproject's outer tracking run remains separate from the model selection run.
        stage_env = os.environ.copy()
        project_run_id = stage_env.pop("MLFLOW_RUN_ID", None)
        if project_run_id:
            report["mlproject_run_id"] = project_run_id
        try:
            feedback = snapshot_feedback(root, feedback_file)
            write_json(root / "runtime/reviewed_feedback.json", {"rows": feedback})
            report["reviewed_training_rows"] = len(feedback)
            for stage in ("test", "prepare", "train", "evaluate"):
                entry = {"name": stage, "status": "running"}
                report["stages"].append(entry)
                write_json(path, report)
                started = time.perf_counter()
                log_path = runs / f"{run_id}-{stage}.log"
                with log_path.open("w") as stream:
                    try:
                        result = subprocess.run(
                            ["dvc", "repro", stage],
                            stdout=stream,
                            stderr=subprocess.STDOUT,
                            timeout=600,
                            env=stage_env,
                        )
                    except subprocess.TimeoutExpired as exc:
                        entry.update(
                            status="failed", seconds=time.perf_counter() - started
                        )
                        raise RuntimeError(
                            f"{stage} timed out; see {log_path.relative_to(root)}"
                        ) from exc
                executed = f"Running stage '{stage}'" in log_path.read_text()
                entry.update(
                    status="passed" if result.returncode == 0 else "failed",
                    seconds=time.perf_counter() - started,
                    mode="executed" if executed else "reused",
                )
                write_json(path, report)
                if result.returncode:
                    raise RuntimeError(
                        f"{stage} failed. See {log_path.relative_to(root)}"
                    )
            evaluation = read_json(root / "artifacts/metrics.json")
            if not evaluation["quality_gate_passed"]:
                raise RuntimeError("The candidate did not pass the quality gate.")
            uri = (root / "artifacts/approved_uri.txt").read_text().strip()
            promote(root, uri)
            report.update(status="passed", approved_uri=uri)
            print(f"Pipeline {run_id} passed. Approved {uri}", flush=True)
        except Exception as exc:
            report.update(status="failed", error=str(exc))
            raise
        finally:
            report["seconds"] = time.perf_counter() - all_start
            write_json(path, report)
            # Prune pipeline status and logs only. MLflow version history is retained.
            for old in sorted(runs.glob("*.json"), reverse=True)[10:]:
                for item in runs.glob(old.stem + "*"):
                    item.unlink()


if __name__ == "__main__":
    # Preserve the pipeline entry point used by Compose, MLproject and saved commands.
    from cli import main

    main()
