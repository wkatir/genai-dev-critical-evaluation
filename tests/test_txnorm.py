import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from txnorm.config import ConfigError, load_rules, parse_rules
from txnorm.interface.cli import main
from txnorm.loader import InputError, RawRecord, load_file
from txnorm.metrics import compute_metrics
from txnorm.models import IssueCode, Status
from txnorm.pipeline import process, process_files
from txnorm.query import TransactionFilter, apply_filter, sort_transactions

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
NOW = datetime(2026, 11, 2, tzinfo=UTC)
RULES = load_rules()


def run(*records):
    return process([RawRecord("test.json", i, r) for i, r in enumerate(records)], RULES, now=NOW)


def bank(**overrides):
    record = {"source": "bank_api", "transaction_id": "B-1", "amount": 10, "currency": "USD",
              "status": "completed", "timestamp": "2026-10-01T10:00:00Z"}
    return {**record, **overrides}


def codes(record_result):
    return {i.code for i in record_result.issues}


# --- normalization ----------------------------------------------------------

def test_each_source_is_detected_and_normalized_to_the_same_schema():
    result = run(
        bank(),
        {"provider": "paygate", "id": "pg_1", "amount_cents": 459900, "curr": "mxn", "state": "succeeded", "created": 1790867520},
        {"terminal_id": "T", "ref": "0007", "monto": "1.234,56", "moneda": "$", "estado": "Aprobada", "fecha": "01/10/2026 09:15"},
        {"tx": {"uuid": "w-1", "value": {"amount": "45000", "currency": "COP"}, "status_code": 1, "date": "2026-10-03 18:45:10"}},
    )
    t = [r.transaction for r in result.records]
    assert [x.source for x in t] == ["bank_api", "payment_gateway", "pos_terminal", "mobile_wallet"]
    assert [r.detected_by for r in result.records] == ["marker", "marker", "signature", "signature"]
    assert t[1].amount == Decimal("4599.00") and t[1].currency == "MXN"
    assert t[2].amount == Decimal("1234.56") and t[2].currency == "MXN"  # "$" overridden per source
    assert t[2].source_id == "0007"  # leading zeros kept
    assert t[2].occurred_at == datetime(2026, 10, 1, 15, 15, tzinfo=UTC)  # Mexico City is UTC-6
    assert t[3].occurred_at == datetime(2026, 10, 3, 23, 45, 10, tzinfo=UTC)  # Bogota is UTC-5
    assert all(x.status is Status.COMPLETED for x in t)


@pytest.mark.parametrize("raw, expected", [(1250.5, "1250.50"), ("3,400.00", "3400.00"), (0.1, "0.10"), ("7", "7.00")])
def test_amounts_are_exact_decimals(raw, expected):
    assert run(bank(amount=raw)).records[0].transaction.amount == Decimal(expected)


@pytest.mark.parametrize("raw", ["twelve", "1,2,3", "12.3.4", True, None, "", [1]])
def test_bad_amounts_are_rejected(raw):
    record = run(bank(amount=raw)).records[0]
    assert not record.is_valid
    assert codes(record) & {IssueCode.INVALID_AMOUNT, IssueCode.MISSING_FIELD}


@pytest.mark.parametrize("raw, expected", [("usd", "USD"), ("€", "EUR"), ("US$", "USD"), ("$", "USD")])
def test_currency_normalization(raw, expected):
    assert run(bank(currency=raw)).records[0].transaction.currency == expected


@pytest.mark.parametrize("raw, expected", [("SUCCESS", Status.COMPLETED), ("declined", Status.FAILED),
                                           ("canceled", Status.CANCELLED), ("reversed", Status.REFUNDED)])
def test_status_mapping(raw, expected):
    assert run(bank(status=raw)).records[0].transaction.status is expected


def test_wallet_numeric_status_codes_only_apply_to_wallet():
    wallet = {"tx": {"uuid": "w", "value": {"amount": "1", "currency": "USD"}, "status_code": 2, "date": "2026-10-01"}}
    assert run(wallet).records[0].transaction.status is Status.REFUNDED
    assert IssueCode.UNKNOWN_STATUS in codes(run(bank(status="2")).records[0])


def test_timestamp_offsets_are_converted_to_utc():
    t = run(bank(timestamp="2026-10-02T09:10:00-05:00")).records[0].transaction
    assert t.occurred_at == datetime(2026, 10, 2, 14, 10, tzinfo=UTC)


# --- validation -------------------------------------------------------------

def test_warnings_keep_the_record_valid():
    rounded = run(bank(amount=1500.5, currency="JPY")).records[0]
    assert rounded.is_valid and rounded.transaction.amount == Decimal("1500")  # half-even
    assert codes(rounded) == {IssueCode.AMOUNT_ROUNDED}

    naive = run(bank(timestamp="2026-10-14T16:20:00")).records[0]
    assert naive.is_valid and codes(naive) == {IssueCode.TIMEZONE_ASSUMED}


def test_all_problems_of_a_record_are_reported_at_once():
    record = run(bank(currency="BTC", status=" ", timestamp="yesterday")).records[0]
    assert codes(record) == {IssueCode.UNSUPPORTED_CURRENCY, IssueCode.MISSING_FIELD, IssueCode.INVALID_DATE}


@pytest.mark.parametrize("overrides, code", [
    ({"amount": 0}, IssueCode.NON_POSITIVE_AMOUNT),
    ({"amount": -5}, IssueCode.NON_POSITIVE_AMOUNT),
    ({"timestamp": "2030-01-01T00:00:00Z"}, IssueCode.DATE_OUT_OF_RANGE),
    ({"timestamp": "1999-12-31T00:00:00Z"}, IssueCode.DATE_OUT_OF_RANGE),
    ({"transaction_id": {"x": 1}}, IssueCode.INVALID_ID),
])
def test_business_rules(overrides, code):
    record = run(bank(**overrides)).records[0]
    assert not record.is_valid and code in codes(record)


def test_duplicates_keep_first_valid_occurrence():
    result = run(bank(amount=-1), bank(), bank(), bank(amount=99))
    assert [r.is_valid for r in result.records] == [False, True, False, False]
    assert "exact duplicate" in result.records[2].issues[0].message
    assert "conflicting" in result.records[3].issues[0].message


def test_unknown_and_malformed_records_are_quarantined_not_dropped():
    result = run({"foo": 1}, "not an object", 42)
    assert len(result.rejected) == 3
    assert [r.issues[0].code for r in result.records] == [
        IssueCode.UNKNOWN_SOURCE, IssueCode.MALFORMED_RECORD, IssueCode.MALFORMED_RECORD]


# --- metrics and querying ---------------------------------------------------

def test_metrics_never_mix_currencies():
    m = compute_metrics(run(bank(transaction_id="1", amount=10), bank(transaction_id="2", amount=5, currency="EUR"),
                            bank(transaction_id="3", amount=7, status="pending"), bank(transaction_id="4", amount=-1)))
    assert (m.total_processed, m.valid, m.invalid) == (4, 3, 1)
    assert m.by_currency["USD"].total == Decimal("17.00") and m.by_currency["USD"].completed == Decimal("10.00")
    assert m.by_currency["EUR"].total == Decimal("5.00")
    assert m.by_status["completed"] == 2 and m.by_status["pending"] == 1 and m.by_status["refunded"] == 0


def test_filter_and_sort():
    txns = run(bank(transaction_id="1", amount=50), bank(transaction_id="2", amount=5, status="failed"),
               bank(transaction_id="3", amount=500, currency="EUR")).transactions
    flt = TransactionFilter(currencies={"USD"}, min_amount=Decimal("10"))
    assert [t.source_id for t in apply_filter(txns, flt)] == ["1"]
    assert [t.source_id for t in apply_filter(txns, TransactionFilter(statuses={Status.FAILED}))] == ["2"]
    assert sort_transactions(txns, "amount", descending=True)[0].currency == "USD"  # grouped by currency first


# --- sample data, config, loader, CLI ----------------------------------------

def test_sample_data_matches_documented_expectations():
    result = process_files([DATA / "valid_transactions.json", DATA / "edge_cases.json"], RULES, now=NOW)
    valid_file = [r for r in result.records if r.origin == "valid_transactions.json"]
    assert all(r.is_valid for r in valid_file)
    for r in result.records:
        note = r.raw.get("_note", "") if isinstance(r.raw, dict) else str(r.raw)
        if not note:
            continue
        expected_code = note.split(":")[0].split(" ")[0]
        if expected_code == "VALID":
            assert r.is_valid, r.ref
        else:
            assert expected_code in {i.code.value for i in r.issues}, (r.ref, note)


def test_invalid_config_fails_fast():
    with pytest.raises(ConfigError, match="unsupported currency"):
        parse_rules({"currencies": {"USD": 2}, "currency_aliases": {"€": "EUR"}, "statuses": {}, "sources": {"x": {}}})
    with pytest.raises(ConfigError, match="mapped to both"):
        parse_rules({"currencies": {"USD": 2}, "statuses": {"completed": ["ok"], "failed": ["ok"]}, "sources": {"x": {}}})


def test_loader_reports_broken_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('[{"a": 1},', encoding="utf-8")
    with pytest.raises(InputError, match="line 1"):
        load_file(bad)


def test_cli_summary_and_export(tmp_path, capsys):
    assert main([str(DATA / "valid_transactions.json"), "--summary", "--export", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))["valid"] == 20
    assert (tmp_path / "normalized_transactions.csv").exists()


def test_cli_interactive_session(monkeypatch, capsys):
    answers = iter(["1", "", "3", "completed", "", "", "", "", "", "", "", "2", "b", "5", "pos_terminal:000451", "",
                    "6", "", "1", "", "", "7", "", "8", "", "4", "x", "q"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "Totals by currency" in out and "Rejected records" in out and "Unknown option" in out
    assert "1234.56" in out and "'1.234,56' parsed" in out  # detail shows normalized value and the rule
