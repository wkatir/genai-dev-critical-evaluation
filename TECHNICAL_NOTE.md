# Technical Note

Wilmer Salazar · txnorm, multi-source transaction normalization

## Design decisions

Every valid record becomes a `Transaction`: a typed, immutable object with a Decimal amount,
an ISO currency, one of five statuses and a UTC timestamp. Before that, each input record is
processed into a wider envelope (`RecordResult`) that also keeps the raw input, the detected
source, the issues found and a note per field describing which rule produced its value. Only
records without errors are promoted to a `Transaction`, so the rest of the code never handles
half-parsed data, and the interface can explain why a record was accepted or rejected.

Everything that depends on the shape of a source lives in `config/rules.toml`: field
locations, number and date formats, time zones, the status mapping and the supported
currencies. The file is validated at startup, so a broken rule stops the program with a clear
message instead of silently producing wrong data.

Invalid records are never deleted. They go to a quarantine list with every problem found and
the original input, and they are left out of all totals. Rounded amounts and assumed time
zones are warnings: the record stays valid but is flagged. Totals are reported per currency
only, because the project has no source of exchange rates. The interface only displays data;
detection, normalization, validation, metrics and filtering live in separate modules.

## Use of AI

Claude Code was used as a programming partner. It proposed the module structure, wrote first
versions of the parsers, the interface and the sample data, and ran the tests after each
change. Several of its initial suggestions were changed after review:

- Amounts were first parsed as floats. They were moved to Decimal, because floating point
  cannot represent most cent values exactly.
- `$` was mapped to USD for every source. Per-source aliases were added, since the POS
  terminals operate in Mexico.
- The amount parser tried to guess the decimal separator. It now uses the separator declared
  for the source and rejects anything ambiguous, such as `1234.56` from a comma-decimal source.
- Refunds were modelled as negative amounts. Amounts are now always positive and the status
  carries the direction.
- Validation stopped at the first error. It now collects every problem in the record.
- The first metrics draft added all amounts into one total. Totals are now kept per currency.

Two interface bugs came up while testing by hand: the `[n]ext` prompt disappeared because the
terminal library treated it as markup, and one invalid filter value cancelled the remaining
filters. Both were fixed and are covered by tests. The suite has 40 tests, including one that
checks the expected outcome written in each record of `data/edge_cases.json`.
