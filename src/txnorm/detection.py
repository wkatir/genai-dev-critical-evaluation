"""Detect which source produced a raw record."""

from __future__ import annotations

from typing import Any

from .config import Rules, SourceRules

_MISSING = object()


def get_path(record: Any, path: str, default: Any = None) -> Any:
    """Resolve a dot path ("customer.id") inside nested dicts."""
    current = record
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def has_path(record: Any, path: str) -> bool:
    return get_path(record, path, _MISSING) is not _MISSING


class DetectionResult:
    def __init__(self, source: SourceRules | None, method: str | None, candidates: list[str] | None = None):
        self.source = source
        self.method = method
        self.candidates = candidates or []


def detect_source(record: dict[str, Any], rules: Rules) -> DetectionResult:
    """1) An explicit marker field wins. 2) Otherwise the source whose whole
    signature is present; if several match, the most specific (longest
    signature) wins; a tie is ambiguous and the record is rejected."""
    for source in rules.sources.values():
        if source.marker and str(get_path(record, source.marker.path)) == source.marker.equals:
            return DetectionResult(source, "marker")

    matches = [s for s in rules.sources.values() if all(has_path(record, p) for p in s.signature)]
    if not matches:
        return DetectionResult(None, None)
    best = max(len(s.signature) for s in matches)
    top = [s for s in matches if len(s.signature) == best]
    if len(top) > 1:
        return DetectionResult(None, None, [s.name for s in top])
    return DetectionResult(top[0], "signature")
