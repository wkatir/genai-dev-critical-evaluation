"""Aggregate metrics over a processing result.

Amounts are never added across currencies: no exchange-rate source is
available, so totals are always reported per currency.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .models import Status, Transaction
from .pipeline import ProcessingResult


@dataclass
class CurrencyTotals:
    count: int = 0
    total: Decimal = Decimal(0)  # all valid transactions, any status
    completed: Decimal = Decimal(0)  # settled money only

    def add(self, txn: Transaction) -> None:
        self.count += 1
        self.total += txn.amount
        if txn.status is Status.COMPLETED:
            self.completed += txn.amount


@dataclass
class Metrics:
    total_processed: int
    valid: int
    invalid: int
    with_warnings: int
    by_status: dict[str, int]
    by_currency: dict[str, CurrencyTotals]
    by_source: dict[str, dict[str, int]] = field(default_factory=dict)
    issues_by_code: dict[str, int] = field(default_factory=dict)

    @property
    def valid_rate(self) -> float:
        return self.valid / self.total_processed if self.total_processed else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_processed": self.total_processed,
            "valid": self.valid,
            "invalid": self.invalid,
            "valid_rate": round(self.valid_rate, 4),
            "with_warnings": self.with_warnings,
            "by_status": self.by_status,
            "by_currency": {
                c: {"count": t.count, "total": str(t.total), "completed": str(t.completed)}
                for c, t in self.by_currency.items()
            },
            "by_source": self.by_source,
            "issues_by_code": self.issues_by_code,
        }


def currency_totals(transactions: list[Transaction]) -> dict[str, CurrencyTotals]:
    totals: dict[str, CurrencyTotals] = {}
    for txn in transactions:
        totals.setdefault(txn.currency, CurrencyTotals()).add(txn)
    return dict(sorted(totals.items()))


def compute_metrics(result: ProcessingResult) -> Metrics:
    txns = result.transactions
    status_counts = Counter(t.status.value for t in txns)

    by_source: dict[str, dict[str, int]] = {}
    for r in result.records:
        stats = by_source.setdefault(r.source or "(undetected)", {"total": 0, "valid": 0, "invalid": 0})
        stats["total"] += 1
        stats["valid" if r.is_valid else "invalid"] += 1

    return Metrics(
        total_processed=len(result.records),
        valid=len(txns),
        invalid=len(result.rejected),
        with_warnings=sum(1 for r in result.valid if r.warnings),
        # status counts cover valid transactions; every status is listed, even at zero
        by_status={s.value: status_counts.get(s.value, 0) for s in Status},
        by_currency=currency_totals(txns),
        by_source=dict(sorted(by_source.items())),
        issues_by_code=dict(Counter(i.code.value for r in result.records for i in r.issues).most_common()),
    )
