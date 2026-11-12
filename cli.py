"""VM commands for training, verification, demonstrations and rollback."""

import argparse
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from data_pipeline import prepare
from training import train, evaluate
from pipeline import run_pipeline, rollback

API = "http://127.0.0.1:8000"


def request(path, method="GET", payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = Request(
        API + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urlopen(req, timeout=20) as response:
        return json.load(response)


def demo():
    pipeline = request("/api/pipeline")
    serving = pipeline.get("serving") or {}
    if serving.get("status") != "ready" or pipeline["running"]:
        raise SystemExit("Wait for a ready model and an idle pipeline.")
    retraining = pipeline.get("retraining") or {}
    if time.time() - retraining.get("last_attempt_at", 0) < 3600:
        raise SystemExit(
            "The default one-hour automatic retraining cooldown is active."
        )
    original = serving["active_uri"]
    demo_id = uuid.uuid4().hex[:8]
    print(f"Original model {original}", flush=True)
    for index in range(52):
        # Unfamiliar tokens and longer queries reliably shift both monitored features.
        text = (
            "Card purchase was declined in xyzzynoveltoken qqqznovel "
            f"quxnovel chzznovel phase bankflowdemo{demo_id}{index} "
        ) * 2
        result = request("/api/tickets", "POST", {"text": text})
        if index < 5:
            request(
                f"/api/tickets/{result['id']}",
                "PATCH",
                {"category": "declined_card_payment"},
            )
    print(
        "Added 52 synthetic tickets and saved five reviewed labels. Waiting for deployment.",
        flush=True,
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        pipeline = request("/api/pipeline")
        serving = pipeline.get("serving") or {}
        if serving.get("status") == "ready" and serving["active_uri"] != original:
            print(f"Updated model {serving['active_uri']}")
            print(
                f"Automatic trigger {(pipeline.get('retraining') or {}).get('last_reason')}"
            )
            return
        time.sleep(3)
    raise SystemExit(
        "The new model is still unavailable. Inspect Operations and runtime/runs."
    )


def check():
    compose = ["docker", "compose", "run", "--rm", "--no-deps", "-T"]
    out = Path("runtime/verification")
    out.mkdir(parents=True, exist_ok=True)
    name = f"bankflow-training-{os.getpid()}"
    with (out / "trainer-resources.tsv").open("w") as samples, subprocess.Popen(
        compose + ["--name", name, "trainer"], stdin=subprocess.DEVNULL
    ) as training:
        samples.write("time_utc\tcpu_percent\tmemory_usage_and_limit\n")
        while training.poll() is None:
            result = subprocess.run(
                [
                    "docker",
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{.CPUPerc}}|{{.MemUsage}}",
                    name,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                usage = result.stdout.strip().replace("|", "\t")
                samples.write(f"{stamp}\t{usage}\n")
                samples.flush()
            time.sleep(1)
        if training.returncode:
            raise SystemExit(training.returncode)
    print("Tests and DVC pipeline passed.")


def verify():
    stack = ["docker", "compose", "--profile", "ai", "--profile", "monitoring"]
    out = Path("runtime/verification")
    out.mkdir(parents=True, exist_ok=True)
    checks = {
        "dvc-dag.txt": ["run", "--rm", "--no-deps", "trainer", "dvc", "dag"],
        "mlproject.log": [
            "run",
            "--rm",
            "--no-deps",
            "trainer",
            "mlflow",
            "run",
            ".",
            "--env-manager",
            "local",
            "--experiment-name",
            "BankFlow Pipeline Runs",
        ],
        "prometheus-config.txt": [
            "exec",
            "-T",
            "prometheus",
            "promtool",
            "check",
            "config",
            "/etc/prometheus/prometheus.yml",
        ],
        "prometheus-rules.txt": [
            "exec",
            "-T",
            "prometheus",
            "promtool",
            "test",
            "rules",
            "/etc/prometheus/alerts.test.yml",
        ],
        "services.txt": ["ps"],
    }
    for name, command in checks.items():
        result = subprocess.run(
            stack + command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        (out / name).write_text(result.stdout)
        print(result.stdout, end="")
        result.check_returncode()

    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        try:
            with urlopen("http://127.0.0.1:8000/api/pipeline", timeout=5) as response:
                pipeline = json.load(response)
            with urlopen(
                "http://127.0.0.1:9090/api/v1/query?query=probe_success", timeout=5
            ) as response:
                probes = json.load(response)["data"]["result"]
            serving = pipeline.get("serving") or {}
            evaluation = pipeline.get("metrics") or {}
            if (
                serving.get("status") == "ready"
                and evaluation.get("quality_gate_passed")
                and len(probes) == 5
                and all(probe["value"][1] == "1" for probe in probes)
            ):
                for name, value in [("pipeline", pipeline), ("probes", probes)]:
                    (out / f"{name}.json").write_text(
                        json.dumps(value, indent=2) + "\n"
                    )
                print(f"Quality gate passed. Active model {serving['active_uri']}")
                print("All five service probes returned 1.")
                break
        except (URLError, TimeoutError):
            pass
        time.sleep(3)
    else:
        raise SystemExit(
            "Timed out waiting for the approved model and five successful probes. Check Compose logs."
        )

    services = subprocess.check_output(stack + ["ps", "-q"], text=True).split()
    snapshots = {
        "container-resources.txt": ["docker", "stats", "--no-stream", *services],
        "vm-memory.txt": ["free", "-h"],
        "vm-disk.txt": ["df", "-h", "."],
        "architecture.txt": ["uname", "-m"],
        "source-commit.txt": ["git", "rev-parse", "HEAD"],
    }
    for name, command in snapshots.items():
        (out / name).write_text(subprocess.check_output(command, text=True))
    print(f"Verification passed. Evidence saved in {out}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "run",
            "prepare",
            "train",
            "evaluate",
            "rollback",
            "check",
            "verify",
            "demo",
        ),
        default="run",
    )
    parser.add_argument(
        "--feedback-file", type=Path, help="Replay a saved MLflow feedback snapshot."
    )
    args = parser.parse_args()
    if args.command == "run":
        run_pipeline(args.feedback_file)
    elif args.command == "rollback":
        try:
            print("Rollback requested:", rollback(Path.cwd()))
        except BlockingIOError as exc:
            raise SystemExit(
                "Wait for the active training pipeline before rolling back."
            ) from exc
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        {
            "prepare": prepare,
            "train": train,
            "evaluate": evaluate,
            "check": check,
            "verify": verify,
            "demo": demo,
        }[args.command]()


if __name__ == "__main__":
    main()
