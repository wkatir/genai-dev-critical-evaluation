"""Filtering and sorting of normalized transactions (UI-independent)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .models import Status, Transaction

SORT_KEYS = {
    "date": lambda t: t.occurred_at,
    "amount": lambda t: (t.currency, t.amount),
    "status": lambda t: t.status.value,
    "source": lambda t: (t.source, t.source_id),
}


@dataclass
class TransactionFilter:
    statuses: set[Status] = field(default_factory=set)
    currencies: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    date_from: date | None = None
    date_to: date | None = None  # inclusive
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    text: str | None = None  # searched in id, description and customer

    def matches(self, t: Transaction) -> bool:
        day = t.occurred_at.date()
        if self.statuses and t.status not in self.statuses:
            return False
        if self.currencies and t.currency not in self.currencies:
            return False
        if self.sources and t.source not in self.sources:
            return False
        if self.date_from and day < self.date_from:
            return False
        if self.date_to and day > self.date_to:
            return False
        if self.min_amount is not None and t.amount < self.min_amount:
            return False
        if self.max_amount is not None and t.amount > self.max_amount:
            return False
        if self.text:
            haystack = " ".join(filter(None, [t.id, t.description, t.customer_id])).lower()
            if self.text.lower() not in haystack:
                return False
        return True

    def is_empty(self) -> bool:
        return self == TransactionFilter()

    def describe(self) -> list[str]:
        parts = []
        if self.statuses:
            parts.append("status in " + ", ".join(sorted(s.value for s in self.statuses)))
        if self.currencies:
            parts.append("currency in " + ", ".join(sorted(self.currencies)))
        if self.sources:
            parts.append("source in " + ", ".join(sorted(self.sources)))
        if self.date_from or self.date_to:
            parts.append(f"date {self.date_from or '…'} to {self.date_to or '…'}")
        if self.min_amount is not None or self.max_amount is not None:
            low = self.min_amount if self.min_amount is not None else "…"
            high = self.max_amount if self.max_amount is not None else "…"
            parts.append(f"amount {low} to {high}")
        if self.text:
            parts.append(f"text contains '{self.text}'")
        return parts


def apply_filter(transactions: list[Transaction], flt: TransactionFilter) -> list[Transaction]:
    return [t for t in transactions if flt.matches(t)]


def sort_transactions(transactions: list[Transaction], key: str = "date", descending: bool = False) -> list[Transaction]:
    if key not in SORT_KEYS:
        raise ValueError(f"unknown sort key '{key}' (valid: {', '.join(SORT_KEYS)})")
    return sorted(transactions, key=SORT_KEYS[key], reverse=descending)
