"""Isolated database and HTTP fixtures shared by the test suite."""

import json
from zipfile import ZipFile
import pytest
from fastapi.testclient import TestClient
import app as api
from ledger import Ledger
from data_pipeline import DATASET


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.sqlite3")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    monkeypatch.setattr(api, "ledger", Ledger(tmp_path / "bank.sqlite3"))
    path = tmp_path / DATASET
    path.parent.mkdir(parents=True)
    with ZipFile(path, "w") as dataset:
        dataset.writestr("categories.json", json.dumps(["declined_card_payment"]))
    with TestClient(api.app) as c:
        yield c
