"""Transactional SQLite ledger, audit history and support-ticket persistence."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from routing import queue_for


class RuleError(ValueError):
    pass


class Ledger:
    """Append-only money movements and separately reviewable support predictions."""

    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL, kind TEXT NOT NULL, amount_paise INTEGER NOT NULL,
                savings_delta INTEGER NOT NULL, card_delta INTEGER NOT NULL,
                memo TEXT NOT NULL, reverses INTEGER UNIQUE REFERENCES transactions(id),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY, text TEXT NOT NULL, category TEXT NOT NULL,
                confidence REAL NOT NULL, queue TEXT NOT NULL, needs_review INTEGER NOT NULL,
                corrected_category TEXT, model_uri TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            """
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def balances(db):
        row = db.execute(
            "SELECT COALESCE(SUM(savings_delta),0) AS balance_paise, "
            "COALESCE(SUM(card_delta),0) AS card_due_paise FROM transactions"
        ).fetchone()
        result = dict(row)
        result["card_limit_paise"] = 5000000
        result["card_available_paise"] = 5000000 - result["card_due_paise"]
        return result

    def snapshot(self):
        with self.connect() as db:
            db.execute("BEGIN")
            result = self.balances(db)
            result["transactions"] = [
                dict(r)
                for r in db.execute(
                    "SELECT t.*, EXISTS(SELECT 1 FROM transactions r WHERE r.reverses=t.id) AS reversed "
                    "FROM transactions t ORDER BY id DESC LIMIT 100"
                )
            ]
            for row in result["transactions"]:
                row.pop("payload")
                row.pop("request_key")
            return result

    def transact(self, kind, amount_paise, key, memo="", reverses=None):
        """Check retry identity, balances and the insert under one writer lock."""
        if (
            type(amount_paise) is not int
            or amount_paise <= 0
            or amount_paise > 100000000
        ):
            raise RuleError(
                "Amount must be positive integer paise, at most ₹10,00,000."
            )
        if not key or len(key) > 100:
            raise RuleError("A request key of 1–100 characters is required.")
        payload = json.dumps([kind, amount_paise, memo, reverses])
        # Serialize the retry-key lookup and balance check with the ledger insert.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM transactions WHERE request_key=?", (key,)
            ).fetchone()
            if old:
                if old["payload"] != payload:
                    raise RuleError(
                        "This request key was already used for a different operation."
                    )
                return {
                    "transaction_id": old["id"],
                    "replayed": True,
                    **self.balances(db),
                }
            sd, cd = 0, 0
            if kind == "deposit":
                sd = amount_paise
            elif kind == "withdrawal":
                sd = -amount_paise
            elif kind == "card_purchase":
                cd = amount_paise
            elif kind == "card_repayment":
                sd, cd = -amount_paise, -amount_paise
            elif kind == "reversal":
                # Compensating entries preserve the original amount and audit history.
                original = db.execute(
                    "SELECT * FROM transactions WHERE id=?", (reverses,)
                ).fetchone()
                if not original or original["kind"] == "reversal":
                    raise RuleError(
                        "Choose an existing original transaction to reverse."
                    )
                if original["amount_paise"] != amount_paise:
                    raise RuleError("A reversal must match the original amount.")
                if db.execute(
                    "SELECT id FROM transactions WHERE reverses=?", (reverses,)
                ).fetchone():
                    raise RuleError("This transaction has already been reversed.")
                sd, cd = -original["savings_delta"], -original["card_delta"]
            else:
                raise RuleError("Unsupported transaction type.")
            current = self.balances(db)
            if current["balance_paise"] + sd < 0:
                raise RuleError("Insufficient savings balance for this operation.")
            new_due = current["card_due_paise"] + cd
            if new_due < 0:
                raise RuleError("This operation would overpay the credit card.")
            if new_due > current["card_limit_paise"]:
                raise RuleError("Credit-card limit exceeded.")
            cur = db.execute(
                "INSERT INTO transactions(request_key,payload,kind,amount_paise,"
                "savings_delta,card_delta,memo,reverses) VALUES(?,?,?,?,?,?,?,?)",
                (key, payload, kind, amount_paise, sd, cd, memo, reverses),
            )
            return {
                "transaction_id": cur.lastrowid,
                "replayed": False,
                **self.balances(db),
            }

    def add_ticket(self, text, prediction, model_uri):
        with self.connect() as db:
            cur = db.execute(
                "INSERT INTO tickets(text,category,confidence,queue,needs_review,model_uri) "
                "VALUES(?,?,?,?,?,?)",
                (
                    text,
                    prediction["category"],
                    prediction["confidence"],
                    prediction["queue"],
                    int(prediction["needs_review"]),
                    model_uri,
                ),
            )
            return cur.lastrowid

    def tickets(self):
        with self.connect() as db:
            return [
                {
                    **dict(r),
                    "corrected_queue": (
                        queue_for(r["corrected_category"])
                        if r["corrected_category"]
                        else None
                    ),
                }
                for r in db.execute("SELECT * FROM tickets ORDER BY id DESC LIMIT 100")
            ]

    def correct(self, ticket_id, category):
        """Retain the original prediction while recording a human review label."""
        with self.connect() as db:
            cur = db.execute(
                "UPDATE tickets SET corrected_category=?,needs_review=0 WHERE id=?",
                (category, ticket_id),
            )
            if not cur.rowcount:
                raise RuleError("Ticket not found.")

    def recent_monitoring_rows(self, limit):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT text, category, corrected_category, model_uri "
                    "FROM tickets ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            ]
