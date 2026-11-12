"""Live API acceptance checks. Run after cli.py verify on the Ubuntu VM."""

import argparse
import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from data_pipeline import read_json, write_json

API = "http://127.0.0.1:8000"
STACK = ["docker", "compose", "--profile", "ai", "--profile", "monitoring"]


def http(path, method="GET", body=None, expected=200, base=API):
    request = Request(
        base + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        response = urlopen(request, timeout=20)
    except HTTPError as exc:
        response = exc
    with response:
        payload = response.read().decode()
        if response.status != expected:
            raise AssertionError(
                f"{method} {path}: expected {expected}, got {response.status}: {payload}"
            )
        return json.loads(payload)


def wait_for(predicate, description, timeout=100):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (URLError, TimeoutError):
            pass
        time.sleep(2)
    raise AssertionError("Timed out: " + description)


def active(uri):
    state = http("/api/pipeline").get("serving") or {}
    return state.get("status") == "ready" and state.get("active_uri") == uri


def smoke():
    """Preserve opening balances using linked reversals; retain the audit entries."""
    start = http("/api/account")
    prefix = "acceptance-" + uuid.uuid4().hex
    if start["card_available_paise"] < 30000:
        raise AssertionError(
            "The demo needs ₹300 of available credit. Use a fresh demo checkout."
        )
    created = []
    for index, (kind, amount) in enumerate(
        [
            ("deposit", 100000),
            ("withdrawal", 20000),
            ("card_purchase", 30000),
            ("card_repayment", 10000),
        ]
    ):
        body = {
            "kind": kind,
            "amount_paise": amount,
            "request_key": f"{prefix}-{index}",
            "memo": "Acceptance demo",
        }
        result = http("/api/transactions", "POST", body)
        replay = http("/api/transactions", "POST", body)
        assert (
            replay["replayed"] and replay["transaction_id"] == result["transaction_id"]
        )
        created.append(result["transaction_id"])
    for identifier in reversed(created):
        http(
            f"/api/transactions/{identifier}/reverse",
            "POST",
            {"request_key": f"{prefix}-reverse-{identifier}"},
        )
    end = http("/api/account")
    assert (end["balance_paise"], end["card_due_paise"]) == (
        start["balance_paise"],
        start["card_due_paise"],
    )
    http(
        "/api/transactions",
        "POST",
        {"kind": "deposit", "amount_paise": 0, "request_key": prefix + "-invalid"},
        expected=422,
    )
    prediction = http("/api/tickets", "POST", {"text": "My card payment was declined."})
    assert re.fullmatch("[a-f0-9]{32}", prediction["model_run_id"])
    assert (
        prediction["category"] in http("/api/categories")
        and 0 <= prediction["confidence"] <= 1
    )
    http(
        f"/api/tickets/{prediction['id']}",
        "PATCH",
        {"category": "declined_card_payment"},
    )
    saved = next(t for t in http("/api/tickets") if t["id"] == prediction["id"])
    assert (
        saved["corrected_category"] == "declined_card_payment"
        and saved["corrected_queue"] == "Cards"
    )
    contract = http("/openapi.json")
    assert sum(len(methods) for methods in contract["paths"].values()) == 11
    return {
        "model_run_id": prediction["model_run_id"],
        "ticket_id": prediction["id"],
        "banking_balances_restored": True,
        "ledger_entries_added": 8,
    }


def lifecycle(out):
    """Deliberately exercise retraining, rejection, rollback and failure recovery."""
    from cli import demo

    state = http("/api/pipeline")
    if state["running"]:
        raise AssertionError("Wait for the active pipeline to finish.")
    original = state["serving"]["active_uri"]
    demo()
    second = http("/api/pipeline")["serving"]["active_uri"]
    assert second != original
    result = {"original_uri": original, "retrained_uri": second}
    params = Path("params.yaml")
    original_settings = params.read_text()
    rejected_settings, replacements = re.subn(
        r"(?m)^(\s*minimum_macro_f1:)\s*[^\n]+$", r"\g<1> 1.0", original_settings
    )
    assert replacements == 1
    try:
        params.write_text(rejected_settings)
        command = STACK + ["run", "--rm", "--no-deps", "-T", "trainer"]
        with (out / "gate-rejection.log").open("w") as stream:
            rejected = subprocess.run(
                command, stdout=stream, stderr=subprocess.STDOUT, timeout=700
            )
        metrics = read_json(Path("artifacts/metrics.json"))
        assert rejected.returncode != 0 and not metrics["quality_gate_passed"]
        assert active(second), "A rejected candidate replaced the active model"
        result["gate_rejection_preserved_active"] = True
    finally:
        params.write_text(original_settings)
    subprocess.run(
        STACK
        + [
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "trainer",
            "python",
            "-m",
            "pipeline",
            "rollback",
        ],
        check=True,
        timeout=60,
    )
    wait_for(lambda: active(original), "rollback")
    subprocess.run(
        STACK + ["run", "--rm", "--no-deps", "-T", "trainer"], check=True, timeout=700
    )
    assert active(original), "Cached approval undid the rollback"
    result["rollback_survived_cached_pipeline"] = True

    before = http("/api/tickets")
    try:
        subprocess.run(STACK + ["stop", "model"], check=True, timeout=60)
        http(
            "/api/tickets",
            "POST",
            {"text": "A test while the model is unavailable."},
            expected=503,
        )
        assert http("/api/tickets") == before

        def alert_firing():
            alerts = http("/api/v1/alerts", base="http://127.0.0.1:9090")["data"][
                "alerts"
            ]
            return any(
                a["labels"].get("alertname") == "BankFlowServiceUnavailable"
                and a["labels"].get("instance") == "http://model:5001/ping"
                and a["state"] == "firing"
                for a in alerts
            )

        wait_for(alert_firing, "model-unavailable alert (two-minute hold)", timeout=210)
        result["outage_preserved_tickets_and_fired_alert"] = True
    finally:
        subprocess.run(STACK + ["start", "model"], check=True, timeout=60)
    wait_for(lambda: active(original), "model recovery")
    wait_for(lambda: not alert_firing(), "alert recovery", timeout=60)
    assert (
        http("/api/tickets", "POST", {"text": "My card payment was declined."})[
            "model_run_id"
        ]
        in original
    )
    result["inference_and_alert_recovered"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lifecycle",
        action="store_true",
        help="Also create synthetic drift, retrain, reject, roll back, stop/restart the model and verify its alert.",
    )
    args = parser.parse_args()
    out = Path("runtime/verification")
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checks": {},
    }
    try:
        report["checks"]["live_api"] = smoke()
        print(
            "PASS: live ledger, retries, reversals, inference, review and API contract",
            flush=True,
        )
        if args.lifecycle:
            report["checks"]["lifecycle"] = lifecycle(out)
            print(
                "PASS: retraining, gate rejection, rollback, outage and alert recovery",
                flush=True,
            )
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        write_json(out / "acceptance.json", report)


if __name__ == "__main__":
    main()
