# Technical Note

**Author:** Wilmer Salazar · **Project:** txnorm — multi-source transaction normalization

## Design decisions

1. **Two-level model.** `Transaction` is the strict canonical schema (typed, frozen, only
   built when a record has no errors). `RecordResult` is an audit envelope kept for *every*
   input record: detected source, partial normalized fields, a per-field trace of the rule
   applied, issues and the raw data. This is what lets the interface explain *why* a value
   looks the way it does.
2. **Rules as data.** Status mapping, currencies, aliases, date formats and source field
   mappings live in `config/rules.toml` (TOML chosen because it allows comments and is read
   by the standard library). The file is validated at startup so a bad rule fails loudly.
3. **Money is `Decimal`**, quantized to each currency's decimals; totals are only per
   currency because there is no exchange-rate source.
4. **All timestamps in UTC**, with source-specific local timezones for naive values.
5. **Separate, don't discard.** Invalid records are quarantined with all their issues
   (normalization does not stop at the first error) and excluded from totals; warnings flag
   but keep a record. The first *valid* occurrence of an id wins over duplicates.
6. **Layering.** Loader → detection → normalization → validation → metrics/query; the CLI
   only renders and never contains business logic, so another interface could reuse the core.

## Use of AI

Claude Code (Anthropic) was used as a pair programmer: it proposed the module layout, drafted
the parsers, the CLI and the test data, and ran the test suite after every change. Key
suggestions that were reviewed and adjusted:

| Initial suggestion | Adjustment | Reason |
|---|---|---|
| Parse amounts as `float` | `Decimal` via `repr`, quantized per currency | `0.1 + 0.2 != 0.3`; money must be exact |
| Map `$` to USD globally | Per-source alias override (`$` → MXN for the POS) | `$` is ambiguous across countries |
| Accept `1234.56` in a comma-decimal source | Strict grouping regex, reject as ambiguous | guessing the separator silently corrupts amounts |
| Represent refunds as negative amounts | Amount always `> 0`; direction comes from status | avoids double negation and ambiguous totals |
| Stop at the first error per record | Collect every issue of the record | users fix data faster when they see all problems |
| One grand total of all amounts | Totals per currency, also "completed only" | adding USD and COP is meaningless; failed payments are not money received |
| Treat any repeated id as duplicate | Only a *valid* record claims an id | a corrected resend must be accepted |

Verification: 38 automated tests (parsers, business rules, metrics, filters, config errors,
CLI session) plus a data-driven test that checks the expected outcome written in each
`_note` of `data/edge_cases.json`. A bug found during manual review (the `[n]ext` prompt was
swallowed by the terminal markup) was fixed by escaping the text.
