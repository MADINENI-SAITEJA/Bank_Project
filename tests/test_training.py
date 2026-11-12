"""Data leakage, label routing, reviewed feedback and drift readiness."""

import csv
import io
from zipfile import ZipFile
import pytest
import pandas as pd
from routing import queue_for
from data_pipeline import (
    DATASET,
    clean_splits,
    drift_report,
    profile_dataset,
    reviewed_feedback,
)


@pytest.mark.parametrize(
    ("category", "queue"),
    [
        ("topping_up_by_card", "Top-ups"),
        ("top_up_by_bank_transfer_charge", "Top-ups"),
        ("card_swallowed", "Cash & ATM"),
        ("wrong_exchange_rate_for_cash_withdrawal", "Cash & ATM"),
        ("pin_blocked", "Cards"),
        ("direct_debit_payment_not_recognised", "Transfers"),
        ("receiving_money", "Transfers"),
        ("passcode_forgotten", "Account support"),
    ],
)
def test_queue_routing(category, queue):
    assert queue_for(category) == queue


def frame(rows):
    return pd.DataFrame(rows, columns=["text", "category"])


def test_test_duplicates_are_preserved():
    test = frame([("test query", "a"), ("test query", "a")])
    result, _ = clean_splits({"train": frame([("training query", "a")]), "test": test})
    pd.testing.assert_frame_equal(result["test"], test)


def test_training_deduplication_and_overlap_removal():
    result, removed = clean_splits(
        {
            "train": frame([("Keep", "a"), ("keep", "a"), ("TEST QUERY", "b")]),
            "test": frame([("test query", "b")]),
        }
    )
    assert removed == 1
    assert result["train"].text.tolist() == ["Keep"]
    assert len(result["test"]) == 1


def test_conflicting_training_labels_are_rejected():
    with pytest.raises(ValueError, match="Conflicting labels"):
        clean_splits(
            {
                "train": frame([("same query", "a"), ("SAME QUERY", "b")]),
                "test": frame([("other query", "b")]),
            }
        )


def test_feedback_snapshot_excludes_reference_texts_and_conflicting_labels(tmp_path):
    path = tmp_path / DATASET
    path.parent.mkdir(parents=True)
    with ZipFile(path, "w") as dataset:
        for split, rows in {
            "train": [("public training", "cards"), ("another example", "transfer")],
            "test": [("public holdout", "transfer")],
        }.items():
            csv_buffer = io.StringIO(newline="")
            writer = csv.writer(csv_buffer)
            writer.writerow(["text", "category"])
            writer.writerows(rows)
            dataset.writestr(f"{split}.csv", csv_buffer.getvalue())
    candidates = [
        (" Public TRAINING ", "cards"),
        ("public holdout", "cards"),
        (" A novel issue ", "cards"),
        ("a novel issue", "cards"),
        ("disagreement", "cards"),
        ("disagreement", "transfer"),
        ("invalid label", "unknown"),
    ]
    rows = [{"text": text, "category": category} for text, category in candidates]
    assert reviewed_feedback(tmp_path, rows) == [
        {"text": "a novel issue", "category": "cards"}
    ]


def test_drift_requires_samples_and_agreement_uses_active_version():
    train = pd.DataFrame({"text": ["card declined"], "category": ["cards"]})
    test = pd.DataFrame({"text": ["test exclusive"], "category": ["cards"]})
    profile = profile_dataset({"train": train, "test": test})
    assert "exclusive" not in profile["vocabulary"]
    config = {
        "minimum_samples": 2,
        "length_jsd_threshold": 0.12,
        "unknown_delta_threshold": 0.15,
    }
    recent = [
        {
            "text": "novel " * 20,
            "corrected_category": "cards",
            "category": "cards",
            "model_uri": "active",
        },
        {
            "text": "unfamiliar " * 20,
            "corrected_category": "cards",
            "category": "transfer",
            "model_uri": "previous",
        },
    ]
    assert not drift_report(profile, recent[:1], "active", config)["ready"]
    result = drift_report(profile, recent, "active", config)
    assert result["ready"] and result["drift"]
    assert result["length_jsd"] == pytest.approx(1)
    assert result["unknown_delta"] == pytest.approx(1)
    assert result["reviewed_samples"] == 1 and result["reviewed_agreement"] == 1
