# txnorm — Multi-Source Transaction Normalization & Explorer

Normalizes financial transactions coming from four heterogeneous sources into a single
canonical model, validates them, computes metrics, and lets a human explore the result
through an **interactive CLI** (menus, filters, sorting, paging, detail and quarantine views).

> Technical note (design decisions and use of AI): [TECHNICAL_NOTE.md](TECHNICAL_NOTE.md)

## Quick start

Requires Python 3.11+.

```bash
# with uv (recommended)
uv sync
uv run python -m txnorm                 # loads every file in data/ and opens the menu

# or with pip
python -m venv .venv && .venv\Scripts\activate      # Windows  (source .venv/bin/activate on Linux/macOS)
pip install -r requirements.txt && pip install -e .
python -m txnorm
```

Other modes:

```bash
python -m txnorm data/edge_cases.json            # explore a specific file (several files allowed)
python -m txnorm --summary                       # print metrics as JSON and exit
python -m txnorm --export output                 # write normalized JSON/CSV, rejected records, metrics
python -m txnorm --rules my_rules.toml           # use another rules file
uv run pytest                                    # run the 38 tests
```

## Interactive interface

| Option | What it does |
|---|---|
| 1 Dashboard | Processed / valid / invalid counts, count by status, totals by currency, breakdown by source |
| 2 Browse | Paged table of valid transactions with the active filters; sort by date, amount, status or source; totals of the current selection |
| 3 / 4 Filters | Status, currency, source, date range, amount range, free-text search (id, description, customer) |
| 5 Detail | Normalized fields side by side with **the rule applied to each field**, issues, and the original raw record |
| 6 Quarantine | Rejected records, filterable by issue code, each one inspectable |
| 7 Issue report | Count per issue code, its meaning and the action taken |
| 8 Rules | The active rules file rendered as tables (currencies, aliases, status mapping, sources) |
| 9 Export | Writes `normalized_transactions.json/.csv`, `rejected_records.json`, `metrics.json` |

## Input sources

| Source | Detection | Shape | Particularities |
|---|---|---|---|
| `bank_api` | marker `source = "bank_api"` | flat | ISO 8601 with offset, amounts as number or `"3,400.00"` |
| `payment_gateway` | marker `provider = "paygate"` | flat + `metadata` | amounts in **cents**, epoch seconds, lowercase currency |
| `pos_terminal` | signature `terminal_id, ref, monto, fecha` | flat, Spanish keys | `"1.234,56"`, `dd/mm/yyyy`, local time Mexico City, `$` = MXN |
| `mobile_wallet` | marker `meta.origin` or signature `tx.uuid, tx.value` | nested | numeric status codes, local time Bogotá |

Detection: an explicit marker wins; otherwise the source whose full signature is present.
If several match, the most specific signature wins; a tie is rejected as `AMBIGUOUS_SOURCE`.

## Canonical model

| Field | Type | Rule |
|---|---|---|
| `id` | str | `"{source}:{source_id}"` — ids are only unique within a source |
| `source` | str | detected source key |
| `source_id` | str | original id, kept as text (leading zeros preserved) |
| `amount` | Decimal | major units, quantized to the currency's decimals, always `> 0` |
| `currency` | str | ISO 4217 code from the supported list |
| `status` | enum | `completed`, `pending`, `failed`, `cancelled`, `refunded` |
| `occurred_at` | datetime | timezone-aware, **UTC** |
| `description`, `customer_id` | str \| None | optional, trimmed |

## Rules (`config/rules.toml`)

All transformation rules live in a commented TOML file and are validated at startup
(unknown currency in an alias, a raw status mapped to two canonical statuses, a missing
field mapping, an unknown timezone... all stop the program with a clear message).

- **Amounts**: `Decimal`, never `float`. Strings are parsed with the source's decimal separator and strict thousands grouping (`"1234.56"` in a comma-decimal source is rejected as ambiguous). Minor units are divided by `10^decimals` of the currency. Extra decimals are rounded half-even and flagged.
- **Currency**: ISO code (case-insensitive) or alias (`€`, `US$`, `euro`...). Aliases can be overridden per source because `$` is ambiguous (USD for the bank, MXN for the Mexican POS).
- **Status**: global alias lists per canonical status, plus per-source extras (wallet codes `1`, `0`, `-1`, `2`, `3`).
- **Dates**: each source lists its formats in order (`iso8601`, `epoch_s`, `epoch_ms` or `strptime` patterns). Timestamps without offset take the source's timezone; if it has none, the default one is assumed and flagged. Everything is stored in UTC.

## Validation policy

Invalid records are **separated, never discarded**: they go to a quarantine list with every
issue found (all fields are checked, not only the first error) and the original raw record,
and are excluded from totals. Warnings keep the record valid but flag it.

| Code | Severity | Meaning |
|---|---|---|
| `MALFORMED_RECORD` | error | record is not a JSON object |
| `UNKNOWN_SOURCE` / `AMBIGUOUS_SOURCE` | error | no / several sources match |
| `MISSING_FIELD` | error | required field absent or empty |
| `INVALID_ID` | error | id is not a string or integer |
| `INVALID_AMOUNT` | error | unparseable amount, or non-integer minor units |
| `NON_POSITIVE_AMOUNT` | error | zero or negative — direction is expressed by status |
| `UNSUPPORTED_CURRENCY` | error | currency/alias not configured |
| `UNKNOWN_STATUS` | error | status not in the mapping |
| `INVALID_DATE` | error | matches none of the source's formats |
| `DATE_OUT_OF_RANGE` | error | in the future (5 min skew tolerance) or before 2000-01-01 |
| `DUPLICATE_ID` | error | same `source:id` already accepted (exact or conflicting) |
| `AMOUNT_ROUNDED` | warning | more decimals than the currency allows |
| `TIMEZONE_ASSUMED` | warning | no offset and no source timezone |

Duplicates: the first **valid** occurrence claims the id, so a corrected resend of a
previously invalid record is accepted.

## Metrics

Total processed, valid vs invalid (and rate), valid records with warnings, count by status
(valid records), totals by currency (all valid statuses and *completed* only), breakdown by
source and issues by code. Different currencies are never added together: there is no
exchange-rate source, so any cross-currency total would be invented.

## Test data

- `data/valid_transactions.json` — 20 clean records, 5 per source, every status and currency.
- `data/edge_cases.json` — 23 records; each one has a `_note` with the expected outcome (19 rejected, 4 valid of which 2 with warnings). A test checks every note against the actual result.

## Project structure

```
config/rules.toml            normalization rules (status mapping, currencies, date formats, sources)
data/                        test data
src/txnorm/
  models.py                  canonical Transaction, RecordResult envelope, issue codes
  config.py                  rules loading + validation
  loader.py                  JSON reading
  detection.py               source detection
  normalization.py           field conversion rules (amount, currency, status, dates)
  validation.py              business rules, duplicate policy
  pipeline.py                orchestration
  metrics.py                 aggregates
  query.py                   filtering / sorting (UI-independent)
  export.py                  JSON / CSV output
  interface/cli.py           interactive interface (rich)
tests/test_txnorm.py         unit, data-driven and CLI tests
```
