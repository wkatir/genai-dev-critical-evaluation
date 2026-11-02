"""Canonical data model.

`Transaction` is the normalized schema every source is mapped to. Only records
that pass normalization and validation without errors become a Transaction.
`RecordResult` is the audit envelope kept for *every* input record (valid or
not), so nothing is silently discarded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    COMPLETED = "completed"
    PENDING = "pending"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class Severity(StrEnum):
    ERROR = "error"  # record is rejected (quarantined)
    WARNING = "warning"  # record is accepted but flagged


class IssueCode(StrEnum):
    MALFORMED_RECORD = "MALFORMED_RECORD"
    UNKNOWN_SOURCE = "UNKNOWN_SOURCE"
    AMBIGUOUS_SOURCE = "AMBIGUOUS_SOURCE"
    MISSING_FIELD = "MISSING_FIELD"
    INVALID_ID = "INVALID_ID"
    INVALID_AMOUNT = "INVALID_AMOUNT"
    NON_POSITIVE_AMOUNT = "NON_POSITIVE_AMOUNT"
    UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"
    UNKNOWN_STATUS = "UNKNOWN_STATUS"
    INVALID_DATE = "INVALID_DATE"
    DATE_OUT_OF_RANGE = "DATE_OUT_OF_RANGE"
    DUPLICATE_ID = "DUPLICATE_ID"
    AMOUNT_ROUNDED = "AMOUNT_ROUNDED"
    TIMEZONE_ASSUMED = "TIMEZONE_ASSUMED"


WARNING_CODES = {IssueCode.AMOUNT_ROUNDED, IssueCode.TIMEZONE_ASSUMED}

# Human-readable meaning of each code, shown in the interface and README.
ISSUE_DESCRIPTIONS: dict[IssueCode, str] = {
    IssueCode.MALFORMED_RECORD: "Record is not a JSON object",
    IssueCode.UNKNOWN_SOURCE: "Structure matches no configured source",
    IssueCode.AMBIGUOUS_SOURCE: "Structure matches several sources equally",
    IssueCode.MISSING_FIELD: "A required field is absent or empty",
    IssueCode.INVALID_ID: "Transaction id is not a string or integer",
    IssueCode.INVALID_AMOUNT: "Amount cannot be parsed with the source's number format",
    IssueCode.NON_POSITIVE_AMOUNT: "Amount is zero or negative (direction is given by status)",
    IssueCode.UNSUPPORTED_CURRENCY: "Currency is not supported or alias is unknown",
    IssueCode.UNKNOWN_STATUS: "Status value is not in the status mapping",
    IssueCode.INVALID_DATE: "Date matches none of the source's date formats",
    IssueCode.DATE_OUT_OF_RANGE: "Date is in the future or before the minimum date",
    IssueCode.DUPLICATE_ID: "Same source + id already accepted earlier",
    IssueCode.AMOUNT_ROUNDED: "Amount had more decimals than the currency allows",
    IssueCode.TIMEZONE_ASSUMED: "Timestamp had no offset; default timezone assumed",
}


@dataclass(frozen=True)
class Issue:
    code: IssueCode
    message: str
    field: str | None = None

    @property
    def severity(self) -> Severity:
        return Severity.WARNING if self.code in WARNING_CODES else Severity.ERROR

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "severity": self.severity.value, "field": self.field, "message": self.message}


@dataclass(frozen=True)
class Transaction:
    """A normalized transaction. All fields are typed and already validated."""

    source: str
    source_id: str
    amount: Decimal  # major units, quantized to the currency's decimals; always > 0
    currency: str  # ISO 4217
    status: Status
    occurred_at: datetime  # timezone-aware, UTC
    description: str | None = None
    customer_id: str | None = None

    @property
    def id(self) -> str:
        """Globally unique key: ids are only unique inside their own source."""
        return f"{self.source}:{self.source_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "source_id": self.source_id,
            "amount": str(self.amount),  # string keeps exact decimal precision
            "currency": self.currency,
            "status": self.status.value,
            "occurred_at": self.occurred_at.isoformat().replace("+00:00", "Z"),
            "description": self.description,
            "customer_id": self.customer_id,
        }


@dataclass
class RecordResult:
    """Outcome of processing one raw record."""

    origin: str  # input file name
    index: int  # position inside that file (0-based)
    raw: Any
    source: str | None = None
    detected_by: str | None = None  # "marker" | "signature"
    fields: dict[str, Any] = field(default_factory=dict)  # normalized values (may be partial)
    trace: dict[str, str] = field(default_factory=dict)  # field -> how it was transformed
    issues: list[Issue] = field(default_factory=list)
    transaction: Transaction | None = None

    @property
    def ref(self) -> str:
        return f"{self.origin}#{self.index}"

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def is_valid(self) -> bool:
        return self.transaction is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "source": self.source,
            "detected_by": self.detected_by,
            "issues": [i.to_dict() for i in self.issues],
            "raw": self.raw,
        }
