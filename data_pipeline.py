"""BANKING77 preparation, reference profiles, reviewed feedback and data artifacts."""

import csv
import hashlib
import io
import json
import math
import os
import re
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from zipfile import ZipFile

DATASET = Path("data/banking77.zip")


def parameters(root=Path(".")):
    import yaml

    return yaml.safe_load((root / "params.yaml").read_text())


def dataset_categories(path):
    with ZipFile(path) as dataset:
        return json.loads(dataset.read("categories.json"))


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.close()
            # Readers see the entire new report or the entire previous report.
            temporary.chmod(0o644)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


WORDS = re.compile(r"\b\w+\b")
LENGTH_BINS = ["0–3", "4–7", "8–15", "16+"]


def words(text):
    return WORDS.findall(text.lower())


def distribution(texts, vocabulary):
    lengths = [0] * len(LENGTH_BINS)
    total = unknown = 0
    for text in texts:
        tokens = words(text)
        count = len(tokens)
        lengths[sum(count >= bound for bound in (4, 8, 16))] += 1
        total += count
        unknown += sum(token not in vocabulary for token in tokens)
    return {
        "rows": sum(lengths),
        "length_counts": lengths,
        "mean_words": total / max(sum(lengths), 1),
        "unknown_fraction": unknown / max(total, 1),
    }


def profile_dataset(frames):
    # Use the training vocabulary for both splits so test text never defines features.
    vocabulary = {token for text in frames["train"].text for token in words(text)}
    splits = {}
    for name, frame in frames.items():
        counts = dict(sorted(Counter(frame.category).items()))
        splits[name] = {**distribution(frame.text, vocabulary), "class_counts": counts}
    return {
        "length_bins": LENGTH_BINS,
        "vocabulary": sorted(vocabulary),
        "splits": splits,
    }


def length_jsd(left, right):
    """Jensen-Shannon divergence in bits, with zero-mass terms omitted."""
    totals = [sum(left), sum(right)]
    if not all(totals):
        return 0.0
    score = 0.0
    for a, b in zip(left, right):
        p, q = a / totals[0], b / totals[1]
        midpoint = (p + q) / 2
        for probability in (p, q):
            if probability:
                score += probability * math.log2(probability / midpoint) / 2
    return score


def drift_report(profile, tickets, serving_uri, cfg):
    """Compare recent accepted tickets with the original training distribution."""
    reviewed = [
        row
        for row in tickets
        if row["corrected_category"] and row["model_uri"] == serving_uri
    ]
    agreement = sum(row["category"] == row["corrected_category"] for row in reviewed)
    report = {
        "samples": len(tickets),
        "reviewed_samples": len(reviewed),
        "reviewed_agreement": agreement / len(reviewed) if reviewed else None,
        "length_jsd": None,
        "unknown_delta": None,
        "drift": False,
        "ready": bool(profile) and len(tickets) >= cfg["minimum_samples"],
    }
    if report["ready"]:
        recent = distribution(
            (row["text"] for row in tickets), set(profile["vocabulary"])
        )
        baseline = profile["splits"]["train"]
        jsd = length_jsd(baseline["length_counts"], recent["length_counts"])
        delta = recent["unknown_fraction"] - baseline["unknown_fraction"]
        report.update(
            length_jsd=jsd,
            unknown_delta=delta,
            drift=jsd >= cfg["length_jsd_threshold"]
            or delta >= cfg["unknown_delta_threshold"],
        )
    return report


def reviewed_feedback(root, candidates):
    """Accept novel, consistent human labels without touching public holdouts."""
    allowed = set()
    excluded = set()
    with ZipFile(root / DATASET) as dataset:
        for split in ("train", "test"):
            with dataset.open(f"{split}.csv") as raw:
                for row in csv.DictReader(
                    io.TextIOWrapper(raw, encoding="utf-8", newline="")
                ):
                    excluded.add(row["text"].strip().lower())
                    if split == "train":
                        allowed.add(row["category"])
    groups = defaultdict(set)
    for row in candidates:
        text = row["text"].strip().lower()
        category = row["category"]
        if 3 <= len(text) <= 1000 and text not in excluded and category in allowed:
            groups[text].add(category)
    # Conflicting reviews are excluded, and sort order keeps DVC input hashes stable.
    return [
        {"text": text, "category": next(iter(labels))}
        for text, labels in sorted(groups.items())
        if len(labels) == 1
    ]


def clean(batch):
    if not {"text", "category"}.issubset(batch.columns):
        raise ValueError("Expected text and category columns.")
    if batch[["text", "category"]].isna().any().any():
        raise ValueError("Null text/category found; source must be reviewed.")
    batch = batch.copy()
    batch["text"] = batch["text"].astype(str).str.strip()
    if batch.text.eq("").any():
        raise ValueError("Blank query found.")
    return batch[["text", "category"]]


def clean_splits(frames):
    """Deduplicate training data while preserving every held-out test example."""
    frames = {split: frame.reset_index(drop=True) for split, frame in frames.items()}
    frames["train"] = frames["train"].drop_duplicates().reset_index(drop=True)
    # Only remove overlaps from training, keeping the original held-out distribution.
    test_keys = set(frames["test"].text.str.lower())
    train_keys = frames["train"].text.str.lower()
    removed = int(train_keys.isin(test_keys).sum())
    frames["train"] = frames["train"][~train_keys.isin(test_keys)].reset_index(
        drop=True
    )
    check = (
        frames["train"]
        .assign(key=frames["train"].text.str.lower())
        .groupby("key")
        .category.nunique()
    )
    if check.gt(1).any():
        raise ValueError("Conflicting labels for normalized training text.")
    frames["train"] = (
        frames["train"]
        .loc[~frames["train"].text.str.lower().duplicated()]
        .reset_index(drop=True)
    )
    return frames, removed


def prepare():
    """Validate bundled bytes, transform with Ray and keep the official holdout intact."""
    import ray

    started = time.perf_counter()
    cfg = parameters()["prepare"]
    out = Path("data/prepared")
    out.mkdir(parents=True, exist_ok=True)
    ray.init(
        num_cpus=cfg["cpus"],
        include_dashboard=False,
        _node_ip_address="127.0.0.1",
        object_store_memory=100 * 1024 * 1024,
        _memory=512 * 1024 * 1024,
    )
    try:
        frames = {}
        source_hashes = {}
        with ZipFile(DATASET) as dataset, tempfile.TemporaryDirectory() as directory:
            provenance = json.loads(dataset.read("PROVENANCE.json"))
            for split in ("train", "test"):
                name = f"{split}.csv"
                contents = dataset.read(name)
                source_hashes[name] = hashlib.sha256(contents).hexdigest()
                if source_hashes[name] != provenance["sha256"][name]:
                    raise ValueError(f"Dataset checksum mismatch for {name}.")
                path = Path(directory) / name
                path.write_bytes(contents)
                dataset_split = ray.data.read_csv(str(path)).map_batches(
                    clean, batch_format="pandas"
                )
                frames[split] = dataset_split.to_pandas().reset_index(drop=True)
        frames, removed = clean_splits(frames)
        for split, frame in frames.items():
            frame.to_parquet(out / f"{split}.parquet", index=False)
        write_json(out / "profile.json", profile_dataset(frames))
        elapsed = time.perf_counter() - started
        report = {
            "rows": {k: len(v) for k, v in frames.items()},
            "removed_train_test_overlap": removed,
            "classes": int(frames["train"].category.nunique()),
            "seconds": elapsed,
            "rows_per_second": sum(len(v) for v in frames.values()) / elapsed,
            "source_sha256": source_hashes,
        }
        write_json(out / "prep_metrics.json", report)
        print(json.dumps(report, indent=2))
    finally:
        ray.shutdown()


if __name__ == "__main__":
    prepare()
