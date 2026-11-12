"""Money invariants, audit history, retry safety and ticket reviews."""

import pytest
from concurrent.futures import ThreadPoolExecutor
from ledger import RuleError


def test_opening_balance_is_zero(ledger):
    assert ledger.snapshot()["balance_paise"] == 0
    assert ledger.snapshot()["card_available_paise"] == 5000000


def test_deposit_withdrawal(ledger):
    ledger.transact("deposit", 10000, "a")
    ledger.transact("withdrawal", 2500, "b")
    assert ledger.snapshot()["balance_paise"] == 7500


def test_failed_withdrawal_preserves_state(ledger):
    ledger.transact("deposit", 100, "a")
    with pytest.raises(RuleError, match="Insufficient"):
        ledger.transact("withdrawal", 101, "b")
    snapshot = ledger.snapshot()
    assert snapshot["balance_paise"] == 100 and len(snapshot["transactions"]) == 1


@pytest.mark.parametrize("value", [0, -1, 1.5, True, 100000001])
def test_invalid_amount(ledger, value):
    with pytest.raises(RuleError):
        ledger.transact("deposit", value, "a")
    assert not ledger.snapshot()["transactions"]


def test_retry_is_idempotent(ledger):
    a = ledger.transact("deposit", 100, "same")
    b = ledger.transact("deposit", 100, "same")
    assert a["transaction_id"] == b["transaction_id"] and b["replayed"]
    assert ledger.snapshot()["balance_paise"] == 100


def test_changed_retry_rejected(ledger):
    ledger.transact("deposit", 100, "same")
    with pytest.raises(RuleError, match="different"):
        ledger.transact("deposit", 200, "same")


def test_reversal_keeps_original_and_only_once(ledger):
    a = ledger.transact("deposit", 100, "a")
    r = ledger.transact("reversal", 100, "r", "Reversal", a["transaction_id"])
    assert ledger.snapshot()["balance_paise"] == 0
    assert len(ledger.snapshot()["transactions"]) == 2
    assert ledger.transact("reversal", 100, "r", "Reversal", a["transaction_id"])[
        "replayed"
    ]
    with pytest.raises(RuleError, match="already"):
        ledger.transact("reversal", 100, "r2", "Reversal", a["transaction_id"])
    with pytest.raises(RuleError, match="original"):
        ledger.transact("reversal", 100, "r3", "Reversal", r["transaction_id"])


def test_deposit_reversal_after_spending_rejected(ledger):
    a = ledger.transact("deposit", 100, "a")
    ledger.transact("withdrawal", 90, "b")
    with pytest.raises(RuleError, match="Insufficient"):
        ledger.transact("reversal", 100, "r", "Reversal", a["transaction_id"])
    assert ledger.snapshot()["balance_paise"] == 10


def test_card_purchase_and_repayment(ledger):
    ledger.transact("deposit", 10000, "a")
    ledger.transact("card_purchase", 3000, "b")
    ledger.transact("card_repayment", 2000, "c")
    s = ledger.snapshot()
    assert s["balance_paise"] == 8000 and s["card_due_paise"] == 1000


def test_card_overpayment_and_limit_rejected(ledger):
    ledger.transact("deposit", 10000, "a")
    ledger.transact("card_purchase", 3000, "b")
    with pytest.raises(RuleError, match="overpay"):
        ledger.transact("card_repayment", 3001, "c")
    with pytest.raises(RuleError, match="limit"):
        ledger.transact("card_purchase", 5000000, "d")
    assert ledger.snapshot()["balance_paise"] == 10000


def test_card_repayment_reversal_restores_both(ledger):
    ledger.transact("deposit", 1000, "a")
    ledger.transact("card_purchase", 500, "b")
    p = ledger.transact("card_repayment", 500, "c")
    ledger.transact("reversal", 500, "r", "Reversal", p["transaction_id"])
    assert ledger.snapshot()["balance_paise"] == 1000
    assert ledger.snapshot()["card_due_paise"] == 500


def test_concurrent_duplicate_deposits(ledger):
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(
            pool.map(lambda _: ledger.transact("deposit", 123, "same"), range(12))
        )
    assert sum(not v["replayed"] for v in values) == 1
    assert ledger.snapshot()["balance_paise"] == 123


def test_concurrent_withdrawals_cannot_overdraw(ledger):
    ledger.transact("deposit", 100, "a")

    def withdraw(i):
        try:
            ledger.transact("withdrawal", 80, str(i))
            return True
        except RuleError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(withdraw, [1, 2]))
    assert sum(results) == 1 and ledger.snapshot()["balance_paise"] == 20


def test_review_keeps_original_prediction_and_exposes_corrected_route(ledger):
    ticket = ledger.add_ticket(
        "transfer problem",
        {
            "category": "declined_card_payment",
            "confidence": 0.2,
            "queue": "Cards",
            "needs_review": True,
        },
        "runs:/original/support_model",
    )
    ledger.correct(ticket, "pending_transfer")
    row = ledger.tickets()[0]
    assert row["category"] == "declined_card_payment" and row["queue"] == "Cards"
    assert row["corrected_category"] == "pending_transfer"
    assert row["corrected_queue"] == "Transfers" and not row["needs_review"]
