"""Load and validate the rules file (config/rules.toml).

The config is validated eagerly so that a bad rule fails at startup with a
clear message instead of silently mis-normalizing data later.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Status

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "rules.toml"
REQUIRED_FIELDS = ("source_id", "amount", "currency", "status", "occurred_at")
OPTIONAL_FIELDS = ("description", "customer_id")
SPECIAL_DATE_FORMATS = {"iso8601", "epoch_s", "epoch_ms"}


class ConfigError(Exception):
    """The rules file is missing or inconsistent."""


@dataclass(frozen=True)
class Marker:
    path: str
    equals: str


@dataclass(frozen=True)
class SourceRules:
    name: str
    label: str
    fields: dict[str, str]
    signature: tuple[str, ...]
    amount_unit: str
    decimal_separator: str
    date_formats: tuple[str, ...]
    marker: Marker | None = None
    timezone: ZoneInfo | None = None
    currency_aliases: dict[str, str] | None = None
    status_map: dict[str, Status] | None = None  # source-specific, merged over global


@dataclass(frozen=True)
class Rules:
    version: str
    currencies: dict[str, int]  # ISO code -> decimals
    currency_aliases: dict[str, str]
    status_map: dict[str, Status]  # raw lowercase value -> canonical
    sources: dict[str, SourceRules]
    default_timezone: ZoneInfo
    min_date: date
    max_future_skew_minutes: int
    path: Path | None = None

    def currency_alias(self, source: SourceRules, raw: str) -> str | None:
        key = raw.strip().lower()
        if source.currency_aliases and key in source.currency_aliases:
            return source.currency_aliases[key]
        return self.currency_aliases.get(key)

    def status_for(self, source: SourceRules, raw: str) -> Status | None:
        key = raw.strip().lower()
        if source.status_map and key in source.status_map:
            return source.status_map[key]
        return self.status_map.get(key)


def load_rules(path: str | Path | None = None) -> Rules:
    path = Path(path) if path else DEFAULT_RULES_PATH
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"Rules file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Rules file {path} is not valid TOML: {exc}") from exc
    rules = parse_rules(data)
    return Rules(**{**rules.__dict__, "path": path})


def parse_rules(data: dict[str, Any]) -> Rules:
    currencies = _parse_currencies(data.get("currencies"))
    aliases = _parse_aliases(data.get("currency_aliases", {}), currencies, "currency_aliases")
    status_map = _parse_status_map(data.get("statuses"), "statuses")

    raw_sources = data.get("sources")
    if not isinstance(raw_sources, dict) or not raw_sources:
        raise ConfigError("[sources] must define at least one source")
    sources = {name: _parse_source(name, spec, currencies) for name, spec in raw_sources.items()}

    defaults = data.get("defaults", {})
    validation = data.get("validation", {})
    try:
        min_date = date.fromisoformat(str(validation.get("min_date", "2000-01-01")))
    except ValueError as exc:
        raise ConfigError(f"validation.min_date: {exc}") from exc
    skew = validation.get("max_future_skew_minutes", 5)
    if not isinstance(skew, int) or skew < 0:
        raise ConfigError("validation.max_future_skew_minutes must be a non-negative integer")

    return Rules(
        version=str(data.get("version", "unversioned")),
        currencies=currencies,
        currency_aliases=aliases,
        status_map=status_map,
        sources=sources,
        default_timezone=_zone(defaults.get("timezone", "UTC"), "defaults.timezone"),
        min_date=min_date,
        max_future_skew_minutes=skew,
    )


def _parse_currencies(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("[currencies] must map ISO codes to decimals")
    result = {}
    for code, decimals in raw.items():
        if len(code) != 3 or not code.isalpha() or not code.isupper():
            raise ConfigError(f"currencies.{code}: expected a 3-letter uppercase ISO 4217 code")
        if not isinstance(decimals, int) or not 0 <= decimals <= 4:
            raise ConfigError(f"currencies.{code}: decimals must be an integer between 0 and 4")
        result[code] = decimals
    return result


def _parse_aliases(raw: Any, currencies: dict[str, int], where: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table")
    result = {}
    for alias, code in raw.items():
        if code not in currencies:
            raise ConfigError(f"{where}: alias '{alias}' points to unsupported currency '{code}'")
        result[alias.strip().lower()] = code
    return result


def _parse_status_map(raw: Any, where: str) -> dict[str, Status]:
    if not isinstance(raw, dict):
        raise ConfigError(f"[{where}] must map canonical statuses to lists of raw values")
    result: dict[str, Status] = {}
    for canonical, values in raw.items():
        try:
            status = Status(canonical)
        except ValueError as exc:
            valid = ", ".join(s.value for s in Status)
            raise ConfigError(f"{where}.{canonical}: unknown canonical status (valid: {valid})") from exc
        if not isinstance(values, list):
            raise ConfigError(f"{where}.{canonical} must be a list")
        for value in values:
            key = str(value).strip().lower()
            if key in result and result[key] is not status:
                raise ConfigError(f"{where}: '{value}' is mapped to both {result[key]} and {status}")
            result[key] = status
    return result


def _parse_source(name: str, spec: Any, currencies: dict[str, int]) -> SourceRules:
    where = f"sources.{name}"
    if not isinstance(spec, dict):
        raise ConfigError(f"{where} must be a table")

    fields = spec.get("fields", {})
    missing = [f for f in REQUIRED_FIELDS if f not in fields]
    if missing:
        raise ConfigError(f"{where}.fields is missing required mappings: {', '.join(missing)}")
    unknown = set(fields) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
    if unknown:
        raise ConfigError(f"{where}.fields has unknown canonical fields: {', '.join(sorted(unknown))}")

    signature = spec.get("signature", [])
    if not signature:
        raise ConfigError(f"{where}.signature must list at least one path")

    amount_unit = spec.get("amount_unit", "major")
    if amount_unit not in ("major", "minor"):
        raise ConfigError(f"{where}.amount_unit must be 'major' or 'minor'")
    separator = spec.get("decimal_separator", ".")
    if separator not in (".", ","):
        raise ConfigError(f"{where}.decimal_separator must be '.' or ','")

    date_formats = spec.get("date_formats", [])
    if not date_formats:
        raise ConfigError(f"{where}.date_formats must list at least one format")
    for fmt in date_formats:
        if fmt not in SPECIAL_DATE_FORMATS and "%" not in fmt:
            raise ConfigError(f"{where}.date_formats: '{fmt}' is neither a strptime pattern nor one of {sorted(SPECIAL_DATE_FORMATS)}")

    marker = None
    if "marker" in spec:
        m = spec["marker"]
        if not isinstance(m, dict) or "path" not in m or "equals" not in m:
            raise ConfigError(f"{where}.marker needs 'path' and 'equals'")
        marker = Marker(path=m["path"], equals=str(m["equals"]))

    return SourceRules(
        name=name,
        label=spec.get("label", name),
        fields=dict(fields),
        signature=tuple(signature),
        amount_unit=amount_unit,
        decimal_separator=separator,
        date_formats=tuple(date_formats),
        marker=marker,
        timezone=_zone(spec["timezone"], f"{where}.timezone") if "timezone" in spec else None,
        currency_aliases=_parse_aliases(spec["currency_aliases"], currencies, f"{where}.currency_aliases")
        if "currency_aliases" in spec
        else None,
        status_map=_parse_status_map(spec["status_aliases"], f"{where}.status_aliases") if "status_aliases" in spec else None,
    )


def _zone(name: str, where: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{where}: unknown timezone '{name}'") from exc
