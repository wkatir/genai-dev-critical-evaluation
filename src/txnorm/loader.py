"""Read raw transaction records from JSON files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class InputError(Exception):
    """An input file cannot be read as a list of records."""


@dataclass(frozen=True)
class RawRecord:
    origin: str
    index: int
    data: Any


def load_file(path: str | Path) -> list[RawRecord]:
    """Accepts either a top-level JSON array or an object with a "transactions" array."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise InputError(f"File not found: {path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"Cannot read {path}: {exc}") from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"{path.name} is not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}") from exc

    if isinstance(payload, dict) and isinstance(payload.get("transactions"), list):
        payload = payload["transactions"]
    if not isinstance(payload, list):
        raise InputError(f"{path.name}: expected a JSON array or an object with a 'transactions' array")

    return [RawRecord(origin=path.name, index=i, data=item) for i, item in enumerate(payload)]


def load_files(paths: list[str | Path]) -> list[RawRecord]:
    records: list[RawRecord] = []
    for path in paths:
        records.extend(load_file(path))
    return records
