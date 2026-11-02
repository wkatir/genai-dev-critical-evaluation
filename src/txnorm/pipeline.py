"""Orchestrates load -> detect -> normalize -> validate."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import Rules
from .detection import detect_source
from .loader import RawRecord, load_files
from .models import Issue, IssueCode, RecordResult, Transaction
from .normalization import normalize_fields
from .validation import DuplicateTracker, validate


@dataclass
class ProcessingResult:
    records: list[RecordResult]
    files: list[str] = field(default_factory=list)
    processed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def transactions(self) -> list[Transaction]:
        return [r.transaction for r in self.records if r.transaction is not None]

    @property
    def valid(self) -> list[RecordResult]:
        return [r for r in self.records if r.is_valid]

    @property
    def rejected(self) -> list[RecordResult]:
        return [r for r in self.records if not r.is_valid]

    def find(self, key: str) -> RecordResult | None:
        """Look up by transaction id ("source:id"), bare source id, or ref ("file#index")."""
        key = key.strip().lower()
        for r in self.records:
            if r.ref.lower() == key or (r.transaction and r.transaction.id.lower() == key):
                return r
        matches = [r for r in self.records if str(r.fields.get("source_id", "")).lower() == key]
        return matches[0] if len(matches) == 1 else None


def process_record(raw: RawRecord, rules: Rules, now: datetime, duplicates: DuplicateTracker) -> RecordResult:
    result = RecordResult(origin=raw.origin, index=raw.index, raw=raw.data)

    if not isinstance(raw.data, dict):
        result.issues.append(Issue(IssueCode.MALFORMED_RECORD, f"expected an object, got {type(raw.data).__name__}"))
        return result

    detection = detect_source(raw.data, rules)
    if detection.source is None:
        if detection.candidates:
            result.issues.append(Issue(IssueCode.AMBIGUOUS_SOURCE, f"matches {', '.join(detection.candidates)}"))
        else:
            result.issues.append(Issue(IssueCode.UNKNOWN_SOURCE, f"keys {sorted(raw.data)} match no source signature"))
        return result

    result.source, result.detected_by = detection.source.name, detection.method
    normalize_fields(raw.data, detection.source, rules, result)
    validate(result, rules, now, duplicates)
    return result


def process(raw_records: list[RawRecord], rules: Rules, now: datetime | None = None) -> ProcessingResult:
    now = now or datetime.now(UTC)
    duplicates = DuplicateTracker()
    records = [process_record(r, rules, now, duplicates) for r in raw_records]
    files = list(dict.fromkeys(r.origin for r in raw_records))
    return ProcessingResult(records=records, files=files, processed_at=now)


def process_files(paths: list[str | Path], rules: Rules, now: datetime | None = None) -> ProcessingResult:
    result = process(load_files(paths), rules, now)
    result.files = [Path(p).name for p in paths]
    return result
