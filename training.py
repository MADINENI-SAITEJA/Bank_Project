"""Compare classifiers, log reproducible MLflow runs and gate held-out evaluation."""

import argparse
import csv
import hashlib
import json
import platform
import subprocess
import tempfile
import time
from importlib.metadata import distributions, version
from pathlib import Path
from zipfile import ZipFile
from data_pipeline import parameters, prepare, write_json


def make_model(name, cfg):
    from sklearn.linear_model import LogisticRegression
    from sklearn.multiclass import OneVsRestClassifier
    from sklearn.naive_bayes import MultinomialNB

    if name == "naive_bayes":
        return MultinomialNB(alpha=cfg["nb_alpha"])
    return OneVsRestClassifier(
        LogisticRegression(
            solver="liblinear",
            C=cfg["logistic_C"],
            max_iter=cfg["max_iter"],
            random_state=cfg["seed"],
        ),
        n_jobs=1,
    )


def write_feature_report(model, vectorizer):
    """Report terms with the strongest positive class weights."""
    import numpy as np

    terms = vectorizer.get_feature_names_out()
    if hasattr(model, "feature_log_prob_"):
        weights = model.feature_log_prob_
        weights = weights - weights.mean(axis=0)
        method = "class_log_probability_minus_mean"
    else:
        weights = np.vstack([estimator.coef_[0] for estimator in model.estimators_])
        method = "one_vs_rest_coefficient"
    with Path("artifacts/feature_weights.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(["category", "term", "weight", "method"])
        for category, row in zip(model.classes_, weights):
            for index in np.argsort(row, kind="stable")[-5:][::-1]:
                writer.writerow([category, terms[index], float(row[index]), method])


def train():
    """Select on validation macro-F1, refit and log the model with replayable inputs."""
    import joblib
    import mlflow
    import numpy as np
    import pandas as pd
    from mlflow.models import infer_signature
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics import accuracy_score, f1_score
    from sklearn.model_selection import train_test_split

    from model import SupportModel

    cfg = parameters()["train"]
    Path("artifacts").mkdir(exist_ok=True)
    df = pd.read_parquet("data/prepared/train.parquet")
    a, b = train_test_split(
        df,
        test_size=cfg["validation_fraction"],
        random_state=cfg["seed"],
        stratify=df.category,
    )
    feedback_path = Path("runtime/reviewed_feedback.json")
    feedback = json.loads(feedback_path.read_text())["rows"]
    augmented = pd.concat(
        [a, pd.DataFrame(feedback, columns=["text", "category"])], ignore_index=True
    )
    # Keep validation on the original stratified split. Human labels augment fit data only.
    args = dict(
        max_features=cfg["max_features"], ngram_range=(1, 2), min_df=2, dtype=np.float32
    )
    vec = TfidfVectorizer(**args)
    X = vec.fit_transform(augmented.text)
    V = vec.transform(b.text)
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(
            "Initialize Git and make a commit before training, to preserve provenance."
        ) from exc
    experiment_name = "BankFlow support routing"
    if mlflow.get_experiment_by_name(experiment_name) is None:
        mlflow.create_experiment(
            experiment_name,
            artifact_location=(Path("mlartifacts").resolve() / "bankflow").as_uri(),
        )
    mlflow.set_experiment(experiment_name)
    with mlflow.start_run(run_name="validation-selection") as parent:
        mlflow.log_params(cfg)
        mlflow.set_tag("git_commit", commit)
        dirty = bool(
            subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()
        )
        mlflow.set_tag("working_tree_dirty", str(dirty))
        mlflow.log_dict(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "packages": {
                    dist.metadata["Name"]: version(dist.metadata["Name"])
                    for dist in distributions()
                    if dist.metadata["Name"]
                },
            },
            "provenance/environment.json",
        )
        # A commit alone cannot reproduce uncommitted edits. Save the exact public
        # source/configuration alongside the data hashes and reviewed-label snapshot.
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "source.zip"
            source_files = [
                "app.py",
                "ledger.py",
                "schemas.py",
                "routing.py",
                "model.py",
                "data_pipeline.py",
                "training.py",
                "pipeline.py",
                "cli.py",
                "verify.py",
                "requirements.txt",
                "params.yaml",
                "dvc.yaml",
                "MLproject",
                "Dockerfile",
                "compose.yaml",
                ".gitignore",
                ".dockerignore",
            ]
            for folder in ("frontend", "monitoring", "tests"):
                source_files.extend(
                    str(path)
                    for path in sorted(Path(folder).rglob("*"))
                    if path.is_file()
                    and "__pycache__" not in path.parts
                    and path.suffix
                    in {".py", ".html", ".css", ".js", ".conf", ".yaml", ".json"}
                )
            hashes = {}
            with ZipFile(snapshot, "w") as archive:
                for name in source_files:
                    source = Path(name)
                    if source.is_file():
                        archive.write(source, name)
                        hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
            mlflow.log_artifact(str(snapshot), artifact_path="provenance")
            mlflow.log_dict(
                {"git_commit": commit, "working_tree_dirty": dirty, "sha256": hashes},
                "provenance/source_manifest.json",
            )
        mlflow.log_artifact("data/prepared/prep_metrics.json", artifact_path="data")
        mlflow.log_artifact("params.yaml")
        mlflow.log_artifact("data/prepared/profile.json", artifact_path="data")
        mlflow.log_artifact(str(feedback_path), artifact_path="data")
        mlflow.log_param("reviewed_training_rows", len(feedback))
        mlflow.set_tag(
            "feedback_sha256", hashlib.sha256(feedback_path.read_bytes()).hexdigest()
        )
        results = []
        for name in ["naive_bayes", "logistic"]:
            with mlflow.start_run(run_name=name, nested=True):
                started = time.perf_counter()
                model = make_model(name, cfg)
                model.fit(X, augmented.category)
                pred = model.predict(V)
                result = {
                    "model": name,
                    "accuracy": accuracy_score(b.category, pred),
                    "macro_f1": f1_score(
                        b.category, pred, average="macro", zero_division=0
                    ),
                    "train_seconds": time.perf_counter() - started,
                }
                mlflow.log_param("classifier", name)
                mlflow.log_metrics({k: v for k, v in result.items() if k != "model"})
                results.append(result)
        best = max(results, key=lambda r: r["macro_f1"])["model"]
        # Refit using all eligible training rows only after model selection is complete.
        df = pd.concat(
            [df, pd.DataFrame(feedback, columns=["text", "category"])],
            ignore_index=True,
        )
        vec = TfidfVectorizer(**args)
        X = vec.fit_transform(df.text)
        model = make_model(best, cfg)
        model.fit(X, df.category)
        write_feature_report(model, vec)
        mlflow.log_artifact(
            "artifacts/feature_weights.csv", artifact_path="explainability"
        )
        bundle = {
            "vectorizer": vec,
            "model": model,
            "threshold": cfg["review_threshold"],
            "run_id": parent.info.run_id,
        }
        joblib.dump(bundle, "artifacts/model.joblib", compress=3)
        wrapper = SupportModel()
        wrapper.bundle = bundle
        example = pd.DataFrame({"text": ["My card payment was declined."]})
        output = wrapper.predict(None, example)
        mlflow.pyfunc.log_model(
            artifact_path="support_model",
            python_model=SupportModel(),
            artifacts={"bundle": "artifacts/model.joblib"},
            code_paths=["model.py", "routing.py"],
            signature=infer_signature(example, output),
            input_example=example,
            pip_requirements=[
                "mlflow==2.22.0",
                "scikit-learn==1.5.2",
                "pandas==2.2.3",
                "numpy==1.26.4",
                "joblib==1.4.2",
            ],
        )
        uri = f"runs:/{parent.info.run_id}/support_model"
        Path("artifacts/candidate_uri.txt").write_text(uri + "\n")
        metadata = {
            "run_id": parent.info.run_id,
            "git_commit": commit,
            "selected_model": best,
            "validation": results,
            "train_rows": len(df),
            "features": X.shape[1],
            "reviewed_training_rows": len(feedback),
        }
        Path("artifacts/training.json").write_text(json.dumps(metadata, indent=2))
        mlflow.log_dict(metadata, "training.json")
        mlflow.set_tag("selected_model", best)


def evaluate():
    """Measure the official test split and publish an approval only on a passing gate."""
    import joblib
    import mlflow
    import pandas as pd
    from sklearn.metrics import (
        accuracy_score,
        classification_report,
        confusion_matrix,
        f1_score,
    )

    started = time.perf_counter()
    cfg = parameters()["evaluate"]
    df = pd.read_parquet("data/prepared/test.parquet")
    bundle = joblib.load("artifacts/model.joblib")
    model = bundle["model"]
    X = bundle["vectorizer"].transform(df.text)
    pred = model.predict(X)
    metrics = {
        "test_accuracy": accuracy_score(df.category, pred),
        "test_macro_f1": f1_score(df.category, pred, average="macro", zero_division=0),
        "test_rows": len(df),
        "evaluate_seconds": time.perf_counter() - started,
    }
    passed = bool(metrics["test_macro_f1"] >= cfg["minimum_macro_f1"])
    report = {
        "metrics": metrics,
        "quality_gate_passed": passed,
        "minimum_macro_f1": cfg["minimum_macro_f1"],
        "classification_report": classification_report(
            df.category, pred, output_dict=True, zero_division=0
        ),
    }
    write_json("artifacts/metrics.json", report)
    classes = model.classes_.tolist()
    pd.DataFrame(
        confusion_matrix(df.category, pred, labels=classes),
        index=classes,
        columns=classes,
    ).to_csv("artifacts/confusion_matrix.csv")
    meta = json.loads(Path("artifacts/training.json").read_text())
    with mlflow.start_run(run_id=meta["run_id"]):
        mlflow.log_metrics(metrics)
        mlflow.log_artifact("artifacts/metrics.json", artifact_path="evaluation")
        mlflow.log_artifact(
            "artifacts/confusion_matrix.csv", artifact_path="evaluation"
        )
        mlflow.set_tag("quality_gate_passed", str(passed))
    print(json.dumps(report["metrics"], indent=2))
    if not passed:
        raise RuntimeError("Quality gate failed; existing active deployment retained.")
    # Record a DVC output separately from the live deployment pointer.
    # The pipeline requests deployment only after the entire run succeeds.
    Path("artifacts/approved_uri.txt").write_text(
        Path("artifacts/candidate_uri.txt").read_text()
    )


def main():
    stages = {"prepare": prepare, "train": train, "evaluate": evaluate}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=stages)
    stages[parser.parse_args().stage]()


if __name__ == "__main__":
    main()
