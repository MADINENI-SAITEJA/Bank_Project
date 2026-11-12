"""HTTP contracts, request validation, metrics and inference failures."""

import json
import httpx
import pytest
import app as api


def test_api_flow(client):
    assert client.get("/health").status_code == 200
    payload = {"kind": "deposit", "amount_paise": 1234, "request_key": "d"}
    result = client.post("/api/transactions", json=payload)
    assert result.status_code == 200
    assert client.post("/api/transactions", json=payload).json()["replayed"]
    assert client.get("/api/account").json()["balance_paise"] == 1234
    assert (
        client.post(
            f"/api/transactions/{result.json()['transaction_id']}/reverse",
            json={"request_key": "r"},
        ).status_code
        == 200
    )
    assert client.get("/api/account").json()["balance_paise"] == 0


@pytest.mark.parametrize("amount", [True, "100", 1.2, 0, -1])
def test_api_strict_paise(client, amount):
    assert (
        client.post(
            "/api/transactions",
            json={"kind": "deposit", "amount_paise": amount, "request_key": "a"},
        ).status_code
        == 422
    )


def test_unknown_reverse(client):
    assert (
        client.post(
            "/api/transactions/999/reverse", json={"request_key": "r"}
        ).status_code
        == 404
    )


def test_blank_support_query(client):
    assert client.post("/api/tickets", json={"text": "   "}).status_code == 422


def test_real_api_model_boundary(client, monkeypatch):
    original = httpx.AsyncClient

    def handler(request):
        assert json.loads(request.content)["dataframe_records"] == [
            {"text": "card declined"}
        ]
        return httpx.Response(
            200,
            json={
                "predictions": [
                    {
                        "category": "declined_card_payment",
                        "confidence": 0.8,
                        "queue": "Cards",
                        "needs_review": False,
                        "model_run_id": "1" * 32,
                        "alternatives": "[]",
                    }
                ]
            },
        )

    monkeypatch.setattr(
        api.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
    )
    result = client.post("/api/tickets", json={"text": "card declined"})
    assert result.status_code == 200 and result.json()["queue"] == "Cards"
    ticket = result.json()["id"]
    assert len(client.get("/api/tickets").json()) == 1
    assert (
        client.patch(
            f"/api/tickets/{ticket}", json={"category": "declined_card_payment"}
        ).status_code
        == 200
    )
    assert (
        client.patch(f"/api/tickets/{ticket}", json={"category": "invalid"}).status_code
        == 422
    )


def test_model_failure_does_not_save_ticket(client, monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        api.httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(lambda req: httpx.Response(500)), **kw
        ),
    )
    assert (
        client.post("/api/tickets", json={"text": "card declined"}).status_code == 503
    )
    assert client.get("/api/tickets").json() == []


@pytest.mark.parametrize(
    "change",
    [
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"confidence": "0.8"},
        {"needs_review": "false"},
        {"model_run_id": "unknown"},
        {"queue": "Transfers"},
        {"category": "invented"},
    ],
)
def test_invalid_model_output_does_not_save_ticket(client, monkeypatch, change):
    original = httpx.AsyncClient
    prediction = {
        "category": "declined_card_payment",
        "confidence": 0.8,
        "queue": "Cards",
        "needs_review": False,
        "model_run_id": "1" * 32,
        "alternatives": "[]",
        **change,
    }
    monkeypatch.setattr(
        api.httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"predictions": [prediction]})
            ),
            **kw,
        ),
    )
    assert (
        client.post("/api/tickets", json={"text": "card declined"}).status_code == 503
    )
    assert client.get("/api/tickets").json() == []


def test_api_has_named_response_contracts(client):
    schemas = client.get("/openapi.json").json()["paths"]
    for path in ["/api/account", "/api/tickets", "/api/pipeline"]:
        assert schemas[path]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]


def test_pipeline_disabled_and_metrics(client, monkeypatch):
    monkeypatch.setenv("ALLOW_PIPELINE_RUN", "0")
    assert client.post("/api/pipeline/run").status_code == 403
    assert client.get("/api/pipeline").json()["runs"] == []
    result = client.get("/metrics")
    assert result.status_code == 200 and "bankflow_http_requests_total" in result.text
