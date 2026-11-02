"""Field-level conversion rules: raw source values -> canonical types.

Each parser returns `(value, note)` where `note` explains the transformation
(shown in the interface as the record's trace), or raises `FieldError`.
Normalization never stops at the first problem: every field is attempted so
that a rejected record reports all of its issues at once.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from .config import Rules, SourceRules
from .detection import get_path
from .models import Issue, IssueCode, RecordResult, Status


class FieldError(Exception):
    def __init__(self, code: IssueCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


# --- identifiers and text -------------------------------------------------

def parse_id(value: Any) -> tuple[str, str]:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise FieldError(IssueCode.INVALID_ID, f"id must be a string or integer, got {type(value).__name__}")
    return str(value).strip(), "kept as text (leading zeros preserved)"


def parse_text(value: Any) -> str | None:
    if is_blank(value):
        return None
    return str(value).strip()


# --- amounts --------------------------------------------------------------

def _number_pattern(decimal_sep: str) -> re.Pattern[str]:
    d = re.escape(decimal_sep)
    t = re.escape("," if decimal_sep == "." else ".")
    # Either plain digits or correctly grouped thousands, then optional decimals.
    return re.compile(rf"^[+-]?(\d{{1,3}}({t}\d{{3}})+|\d+)({d}\d+)?$")


def parse_amount(value: Any, source: SourceRules) -> tuple[Decimal, str]:
    """Parse to a Decimal in the source's unit (major or minor). Floats are
    converted through their repr so 0.1 stays 0.1, never 0.1000000000000000055."""
    if isinstance(value, bool):
        raise FieldError(IssueCode.INVALID_AMOUNT, "boolean is not an amount")

    if isinstance(value, int):
        amount, note = Decimal(value), "numeric"
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise FieldError(IssueCode.INVALID_AMOUNT, f"non-finite amount {value}")
        amount, note = Decimal(repr(value)), "numeric (float converted exactly)"
    elif isinstance(value, str):
        text = value.strip().replace(" ", "").replace(" ", "")
        if not _number_pattern(source.decimal_separator).match(text):
            raise FieldError(
                IssueCode.INVALID_AMOUNT,
                f"'{value}' is not a valid number for decimal separator '{source.decimal_separator}'",
            )
        thousands = "," if source.decimal_separator == "." else "."
        amount = Decimal(text.replace(thousands, "").replace(source.decimal_separator, "."))
        note = f"text '{value}' parsed with decimal separator '{source.decimal_separator}'"
    else:
        raise FieldError(IssueCode.INVALID_AMOUNT, f"unsupported amount type {type(value).__name__}")

    if source.amount_unit == "minor" and amount != amount.to_integral_value():
        raise FieldError(IssueCode.INVALID_AMOUNT, f"minor-unit amount must be an integer, got {value}")
    return amount, note


def to_major_units(amount: Decimal, decimals: int, source: SourceRules) -> tuple[Decimal, str | None, bool]:
    """Convert minor units if needed and quantize to the currency's decimals.
    Returns (amount, note, rounded)."""
    note = None
    if source.amount_unit == "minor":
        amount = amount.scaleb(-decimals)
        note = f"minor units / 10^{decimals}"
    exponent = Decimal(1).scaleb(-decimals)
    quantized = amount.quantize(exponent, rounding=ROUND_HALF_EVEN)
    return quantized, note, quantized != amount


# --- currency and status --------------------------------------------------

def parse_currency(value: Any, source: SourceRules, rules: Rules) -> tuple[str, str]:
    if not isinstance(value, str):
        raise FieldError(IssueCode.UNSUPPORTED_CURRENCY, f"currency must be text, got {type(value).__name__}")
    code = value.strip().upper()
    if code in rules.currencies:
        return code, "ISO code" if value == code else f"'{value}' upper-cased"
    alias = rules.currency_alias(source, value)
    if alias:
        scope = "source alias" if source.currency_aliases and value.strip().lower() in source.currency_aliases else "alias"
        return alias, f"'{value}' -> {alias} ({scope})"
    supported = ", ".join(rules.currencies)
    raise FieldError(IssueCode.UNSUPPORTED_CURRENCY, f"'{value}' is not supported (supported: {supported})")


def parse_status(value: Any, source: SourceRules, rules: Rules) -> tuple[Status, str]:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise FieldError(IssueCode.UNKNOWN_STATUS, f"status must be text or a code, got {type(value).__name__}")
    status = rules.status_for(source, str(value))
    if status is None:
        raise FieldError(IssueCode.UNKNOWN_STATUS, f"'{value}' is not in the status mapping")
    return status, f"'{value}' -> {status.value}"


# --- dates ----------------------------------------------------------------

def _parse_with(fmt: str, value: Any) -> datetime:
    if fmt in ("epoch_s", "epoch_ms"):
        if isinstance(value, bool):
            raise ValueError("boolean")
        seconds = float(value)  # accepts numbers and numeric strings
        if fmt == "epoch_ms":
            seconds /= 1000
        return datetime.fromtimestamp(seconds, tz=UTC)
    if not isinstance(value, str):
        raise ValueError("not text")
    if fmt == "iso8601":
        return datetime.fromisoformat(value.strip())
    return datetime.strptime(value.strip(), fmt)


def parse_datetime(value: Any, source: SourceRules, rules: Rules) -> tuple[datetime, str, bool]:
    """Try the source's formats in order. Naive results get the source's
    timezone, or the default one (flagged). Always returns UTC.
    Returns (datetime, note, timezone_assumed)."""
    for fmt in source.date_formats:
        try:
            parsed = _parse_with(fmt, value)
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        assumed = False
        if parsed.tzinfo is None:
            if source.timezone is not None:
                parsed = parsed.replace(tzinfo=source.timezone)
                tz_note = f"source timezone {source.timezone.key}"
            else:
                parsed = parsed.replace(tzinfo=rules.default_timezone)
                tz_note = f"no offset, assumed {rules.default_timezone.key}"
                assumed = True
        else:
            tz_note = "offset in value"
        return parsed.astimezone(UTC), f"format '{fmt}', {tz_note}, converted to UTC", assumed
    formats = ", ".join(source.date_formats)
    raise FieldError(IssueCode.INVALID_DATE, f"'{value}' matches none of the formats: {formats}")


# --- record ---------------------------------------------------------------

def normalize_fields(raw: dict[str, Any], source: SourceRules, rules: Rules, result: RecordResult) -> None:
    """Fill result.fields / result.trace / result.issues for one detected record."""
    values = {name: get_path(raw, path) for name, path in source.fields.items()}

    def fail(name: str, err: FieldError) -> None:
        result.issues.append(Issue(err.code, err.message, name))

    def required(name: str) -> bool:
        if is_blank(values.get(name)):
            path = source.fields[name]
            result.issues.append(Issue(IssueCode.MISSING_FIELD, f"'{path}' is missing or empty", name))
            return False
        return True

    if required("source_id"):
        try:
            result.fields["source_id"], result.trace["source_id"] = parse_id(values["source_id"])
        except FieldError as err:
            fail("source_id", err)

    if required("currency"):
        try:
            result.fields["currency"], result.trace["currency"] = parse_currency(values["currency"], source, rules)
        except FieldError as err:
            fail("currency", err)

    if required("amount"):
        try:
            amount, note = parse_amount(values["amount"], source)
            currency = result.fields.get("currency")
            if currency is not None:
                amount, unit_note, rounded = to_major_units(amount, rules.currencies[currency], source)
                note = f"{note}; {unit_note}" if unit_note else note
                if rounded:
                    result.issues.append(Issue(
                        IssueCode.AMOUNT_ROUNDED,
                        f"{values['amount']} rounded to {amount} ({currency} has {rules.currencies[currency]} decimals)",
                        "amount",
                    ))
                result.fields["amount"] = amount
            # Without a valid currency the minor-unit scale is unknown; the
            # record is already rejected by the currency error.
            result.trace["amount"] = note
        except FieldError as err:
            fail("amount", err)

    if required("status"):
        try:
            result.fields["status"], result.trace["status"] = parse_status(values["status"], source, rules)
        except FieldError as err:
            fail("status", err)

    if required("occurred_at"):
        try:
            when, note, assumed = parse_datetime(values["occurred_at"], source, rules)
            result.fields["occurred_at"], result.trace["occurred_at"] = when, note
            if assumed:
                result.issues.append(Issue(IssueCode.TIMEZONE_ASSUMED, note, "occurred_at"))
        except FieldError as err:
            fail("occurred_at", err)

    for name in ("description", "customer_id"):
        if name in source.fields:
            result.fields[name] = parse_text(values.get(name))
