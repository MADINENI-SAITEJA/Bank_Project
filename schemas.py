"""Validated request, response and inference contracts for the HTTP API."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt


class Transaction(BaseModel):
    kind: Literal["deposit", "withdrawal", "card_purchase", "card_repayment"]
    amount_paise: StrictInt = Field(gt=0, le=100000000)
    request_key: str = Field(min_length=1, max_length=100)
    memo: str = Field(default="", max_length=200)


class Reverse(BaseModel):
    request_key: str = Field(min_length=1, max_length=100)


class Ticket(BaseModel):
    text: str = Field(min_length=3, max_length=1000)


class Correction(BaseModel):
    category: str


class Health(BaseModel):
    status: Literal["ok"]
    mode: Literal["simulator"]


class Balances(BaseModel):
    balance_paise: int
    card_due_paise: int
    card_limit_paise: int
    card_available_paise: int


class TransactionResult(Balances):
    transaction_id: int
    replayed: bool


class LedgerEntry(BaseModel):
    id: int
    kind: str
    amount_paise: int
    savings_delta: int
    card_delta: int
    memo: str
    reverses: int | None
    created_at: str
    reversed: int


class Account(Balances):
    transactions: list[LedgerEntry]


class Prediction(BaseModel):
    # Reject malformed service output before it can reach SQLite or metric labels.
    model_config = ConfigDict(strict=True)
    category: str
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    queue: Literal["Cards", "Transfers", "Cash & ATM", "Top-ups", "Account support"]
    needs_review: bool
    model_run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    alternatives: str


class TicketResult(Prediction):
    id: int


class StoredTicket(BaseModel):
    id: int
    text: str
    category: str
    confidence: float
    queue: str
    needs_review: int
    corrected_category: str | None
    corrected_queue: str | None
    model_uri: str
    created_at: str


class CorrectionResult(BaseModel):
    status: Literal["corrected"]


class PipelineResult(BaseModel):
    running: bool
    runs: list[dict]
    stages: list[str]
    metrics: dict | None
    preparation: dict | None
    monitoring: dict | None
    serving: dict | None
    retraining: dict | None


class PipelineRequestResult(BaseModel):
    status: Literal["requested"]
    note: str
