"""Business rules applied after normalization.

Policy: invalid records are never discarded. They are *separated* into a
quarantine list with their issues and the original raw record, and excluded
from financial totals. Warnings mark a record but keep it valid.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .config import Rules
from .models import Issue, IssueCode, RecordResult, Transaction


class DuplicateTracker:
    """A (source, id) key is claimed by the first *valid* occurrence. An
    invalid record does not claim it, so a corrected resend is accepted."""

    def __init__(self) -> None:
        self._seen: dict[str, tuple[Transaction, str]] = {}

    def check(self, txn: Transaction, ref: str) -> Issue | None:
        previous = self._seen.get(txn.id)
        if previous is None:
            self._seen[txn.id] = (txn, ref)
            return None
        first, first_ref = previous
        kind = "exact duplicate" if first == txn else "conflicting duplicate (content differs)"
        return Issue(IssueCode.DUPLICATE_ID, f"{kind} of {first_ref}", "source_id")


def validate(result: RecordResult, rules: Rules, now: datetime, duplicates: DuplicateTracker) -> None:
    """Apply business rules and, if no error remains, build the Transaction."""
    fields = result.fields

    amount = fields.get("amount")
    if amount is not None and amount <= 0:
        result.issues.append(Issue(
            IssueCode.NON_POSITIVE_AMOUNT,
            f"amount must be > 0, got {amount} (refunds are expressed with status, not sign)",
            "amount",
        ))

    when = fields.get("occurred_at")
    if when is not None:
        latest = now + timedelta(minutes=rules.max_future_skew_minutes)
        earliest = datetime.combine(rules.min_date, datetime.min.time(), tzinfo=UTC)
        if when > latest:
            result.issues.append(Issue(IssueCode.DATE_OUT_OF_RANGE, f"{when.isoformat()} is in the future", "occurred_at"))
        elif when < earliest:
            result.issues.append(Issue(
                IssueCode.DATE_OUT_OF_RANGE, f"{when.date()} is before the minimum date {rules.min_date}", "occurred_at"
            ))

    if result.errors:
        return

    txn = Transaction(
        source=result.source,
        source_id=fields["source_id"],
        amount=fields["amount"],
        currency=fields["currency"],
        status=fields["status"],
        occurred_at=fields["occurred_at"],
        description=fields.get("description"),
        customer_id=fields.get("customer_id"),
    )
    duplicate = duplicates.check(txn, result.ref)
    if duplicate:
        result.issues.append(duplicate)
        return
    result.transaction = txn
