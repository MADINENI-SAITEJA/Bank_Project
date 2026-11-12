"""Test gating, retraining policy and rollback protection."""

import pytest
from pipeline import promote, retrain_reason, rollback
from data_pipeline import read_json, write_json


def test_pipeline_test_failure_cannot_promote_model(tmp_path, monkeypatch):
    import subprocess
    import pipeline

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pipeline, "snapshot_feedback", lambda *args: [])
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(pipeline.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="test failed"):
        pipeline.run_pipeline()
    assert calls == [["dvc", "repro", "test"]]
    assert not (tmp_path / "runtime/deployment.json").exists()
    report = read_json(next((tmp_path / "runtime/runs").glob("*.json")))
    assert report["status"] == "failed" and report["stages"][0]["status"] == "failed"


@pytest.mark.parametrize(
    ("drift", "count", "last_digest", "last_attempt", "expected"),
    [
        (True, 4, "", 0, None),
        (True, 5, "current", 0, None),
        (True, 5, "", 99, None),
        (True, 5, "", 0, "input drift"),
        (False, 5, "", 0, "scheduled review"),
        (False, 5, "", 40, None),
    ],
)
def test_retrain_policy(drift, count, last_digest, last_attempt, expected):
    config = {
        "minimum_reviewed": 5,
        "cooldown_seconds": 10,
        "scheduled_interval_seconds": 75,
    }
    state = {
        "started_at": 10,
        "last_attempt_at": last_attempt,
        "last_attempt_digest": last_digest,
    }
    assert (
        retrain_reason(config, {"drift": drift}, state, count, "current", 100)
        == expected
    )


def test_rollback_requires_real_previous_version_and_survives_cache_reuse(tmp_path):
    first = "runs:/" + "1" * 32 + "/support_model"
    second = "runs:/" + "2" * 32 + "/support_model"
    promote(tmp_path, first)
    write_json(
        tmp_path / "runtime/serving.json",
        {"status": "ready", "active_uri": first, "requested_uri": first},
    )
    with pytest.raises(ValueError, match="previous healthy"):
        rollback(tmp_path)
    promote(tmp_path, second)
    with pytest.raises(ValueError, match="request to finish"):
        rollback(tmp_path)
    write_json(
        tmp_path / "runtime/serving.json",
        {
            "status": "ready",
            "active_uri": second,
            "previous_uri": first,
            "requested_uri": second,
        },
    )
    assert rollback(tmp_path) == first
    promote(tmp_path, second)
    assert read_json(tmp_path / "runtime/deployment.json") == {
        "desired_uri": first,
        "last_approved_uri": second,
    }
