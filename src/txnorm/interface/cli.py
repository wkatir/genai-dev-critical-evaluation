"""Interactive command-line interface (menus, filters, paged tables, detail views).

Run:  python -m txnorm [FILES...] [--rules PATH] [--summary] [--export DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from ..config import DEFAULT_RULES_PATH, ConfigError, Rules, load_rules
from ..export import export_results
from ..loader import InputError
from ..metrics import Metrics, compute_metrics, currency_totals
from ..models import ISSUE_DESCRIPTIONS, WARNING_CODES, IssueCode, RecordResult, Status, Transaction
from ..pipeline import ProcessingResult, process_files
from ..query import SORT_KEYS, TransactionFilter, apply_filter, sort_transactions

DATA_DIR = DEFAULT_RULES_PATH.parents[1] / "data"
PAGE_SIZE = 15
STATUS_STYLE = {
    Status.COMPLETED: "green",
    Status.PENDING: "yellow",
    Status.FAILED: "red",
    Status.CANCELLED: "magenta",
    Status.REFUNDED: "cyan",
}


def fmt_amount(amount: Decimal) -> str:
    return f"{amount:,}"


class App:
    def __init__(self, rules: Rules, console: Console | None = None):
        self.rules = rules
        self.console = console or Console()
        self.result: ProcessingResult | None = None
        self.filter = TransactionFilter()
        self.sort_key, self.descending = "date", False

    # --- helpers ------------------------------------------------------------

    def ask(self, prompt: str, default: str = "") -> str:
        suffix = f" [dim]({default})[/dim]" if default else ""
        try:
            answer = self.console.input(f"[bold cyan]{prompt}[/bold cyan]{suffix}: ").strip()
        except EOFError:
            raise KeyboardInterrupt from None
        return answer or default

    def pause(self) -> None:
        self.ask("Press Enter to continue")

    def load(self, paths: list[str]) -> bool:
        try:
            self.result = process_files(paths, self.rules)
        except InputError as exc:
            self.console.print(f"[red]Could not load data:[/red] {exc}")
            return False
        self.filter = TransactionFilter()
        return True

    @property
    def metrics(self) -> Metrics:
        return compute_metrics(self.result)

    # --- main loop ----------------------------------------------------------

    MENU = [
        ("1", "Dashboard (metrics)"),
        ("2", "Browse transactions (uses filters)"),
        ("3", "Edit filters"),
        ("4", "Clear filters"),
        ("5", "Transaction / record detail"),
        ("6", "Rejected records (quarantine)"),
        ("7", "Issue report"),
        ("8", "Normalization rules"),
        ("9", "Export results"),
        ("L", "Load other file(s)"),
        ("Q", "Quit"),
    ]

    def run(self) -> None:
        actions = {
            "1": self.show_dashboard,
            "2": self.browse,
            "3": self.edit_filters,
            "4": self.clear_filters,
            "5": self.show_detail,
            "6": self.show_rejected,
            "7": self.show_issue_report,
            "8": self.show_rules,
            "9": self.export,
            "l": self.load_prompt,
        }
        try:
            while True:
                self.render_header()
                choice = self.ask("Choose an option").lower()
                if choice == "q":
                    break
                action = actions.get(choice)
                if action is None:
                    self.console.print(f"[red]Unknown option '{choice}'[/red]")
                    continue
                action()
        except KeyboardInterrupt:
            pass
        self.console.print("Bye.")

    def render_header(self) -> None:
        m = self.metrics
        status = (
            f"Files: [bold]{', '.join(self.result.files)}[/bold]   "
            f"Processed: [bold]{m.total_processed}[/bold]   "
            f"Valid: [green]{m.valid}[/green]   Rejected: [red]{m.invalid}[/red]   "
            f"Warnings: [yellow]{m.with_warnings}[/yellow]"
        )
        flt = "; ".join(self.filter.describe()) or "none"
        menu = Table.grid(padding=(0, 2))
        for i in range(0, len(self.MENU), 2):
            cells = [f"[bold]{k}[/bold]  {label}" for k, label in self.MENU[i : i + 2]]
            menu.add_row(*cells)
        self.console.print()
        self.console.print(Panel(
            Group(status, f"Active filters: [italic]{flt}[/italic]", "", menu),
            title="[bold]txnorm - Transaction Explorer[/bold]",
            border_style="blue",
        ))

    # --- views --------------------------------------------------------------

    def show_dashboard(self) -> None:
        m = self.metrics
        overview = Table(title="Overview", box=box.SIMPLE_HEAVY)
        overview.add_column("Metric")
        overview.add_column("Value", justify="right")
        overview.add_row("Total processed", str(m.total_processed))
        overview.add_row("Valid", f"[green]{m.valid}[/green] ({m.valid_rate:.0%})")
        overview.add_row("Invalid (quarantined)", f"[red]{m.invalid}[/red] ({1 - m.valid_rate if m.total_processed else 0:.0%})")
        overview.add_row("Valid with warnings", f"[yellow]{m.with_warnings}[/yellow]")

        statuses = Table(title="Valid transactions by status", box=box.SIMPLE_HEAVY)
        statuses.add_column("Status")
        statuses.add_column("Count", justify="right")
        statuses.add_column("Share", justify="right")
        for name, count in m.by_status.items():
            share = count / m.valid if m.valid else 0
            statuses.add_row(Text(name, style=STATUS_STYLE[Status(name)]), str(count), f"{share:.0%}")

        self.console.print(overview, statuses)
        self.console.print(self.currency_table(m.by_currency, "Totals by currency (valid only, no FX conversion)"))

        sources = Table(title="By source", box=box.SIMPLE_HEAVY)
        for col in ("Source", "Total", "Valid", "Invalid"):
            sources.add_column(col, justify="left" if col == "Source" else "right")
        for name, s in m.by_source.items():
            sources.add_row(name, str(s["total"]), f"[green]{s['valid']}[/green]", f"[red]{s['invalid']}[/red]")
        self.console.print(sources)
        self.pause()

    def currency_table(self, totals, title: str) -> Table:
        table = Table(title=title, box=box.SIMPLE_HEAVY)
        table.add_column("Currency")
        table.add_column("Transactions", justify="right")
        table.add_column("Total (all statuses)", justify="right")
        table.add_column("Completed (settled)", justify="right")
        for currency, t in totals.items():
            table.add_row(currency, str(t.count), fmt_amount(t.total), f"[green]{fmt_amount(t.completed)}[/green]")
        if not totals:
            table.add_row("-", "0", "-", "-")
        return table

    def filtered(self) -> list[Transaction]:
        return sort_transactions(apply_filter(self.result.transactions, self.filter), self.sort_key, self.descending)

    def browse(self) -> None:
        page = 0
        while True:
            rows = self.filtered()
            pages = max(1, -(-len(rows) // PAGE_SIZE))
            page = min(page, pages - 1)
            chunk = rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
            order = "desc" if self.descending else "asc"
            self.console.print(self.transactions_table(
                chunk,
                f"Transactions {page + 1}/{pages} - {len(rows)} of {len(self.result.transactions)} valid "
                f"- sorted by {self.sort_key} {order}",
            ))
            self.console.print(self.currency_table(currency_totals(rows), "Totals for this selection"))
            cmd = self.ask(r"\[n]ext  \[p]rev  \[s]ort  \[f]ilter  \[d]etail  \[b]ack", "b").lower()
            if cmd == "n":
                page = min(page + 1, pages - 1)
            elif cmd == "p":
                page = max(page - 1, 0)
            elif cmd == "s":
                self.change_sort()
            elif cmd == "f":
                self.edit_filters()
                page = 0
            elif cmd == "d":
                self.show_detail()
            elif cmd == "b":
                return

    def transactions_table(self, txns: list[Transaction], title: str) -> Table:
        warned = {r.transaction.id for r in self.result.valid if r.warnings}
        table = Table(title=title, box=box.SIMPLE_HEAVY, show_lines=False)
        table.add_column("ID", no_wrap=True)
        table.add_column("Date (UTC)", no_wrap=True)
        table.add_column("Amount", justify="right")
        table.add_column("Cur")
        table.add_column("Status")
        table.add_column("Customer")
        table.add_column("Description", overflow="ellipsis", max_width=28)
        table.add_column("!", justify="center")
        for t in txns:
            table.add_row(
                t.id,
                t.occurred_at.strftime("%Y-%m-%d %H:%M"),
                fmt_amount(t.amount),
                t.currency,
                Text(t.status.value, style=STATUS_STYLE[t.status]),
                t.customer_id or "-",
                t.description or "-",
                "[yellow]W[/yellow]" if t.id in warned else "",
            )
        if not txns:
            table.add_row("[dim]No transactions match the current filters[/dim]", *[""] * 7)
        return table

    def change_sort(self) -> None:
        key = self.ask(f"Sort by ({'/'.join(SORT_KEYS)})", self.sort_key).lower()
        if key not in SORT_KEYS:
            self.console.print(f"[red]Unknown sort key '{key}'[/red]")
            return
        direction = self.ask("Direction (asc/desc)", "desc" if self.descending else "asc").lower()
        self.sort_key, self.descending = key, direction.startswith("d")

    def edit_filters(self) -> None:
        self.console.print(
            "[dim]Enter keeps the current value, '-' clears it. Lists are comma-separated.[/dim]"
        )
        f = self.filter
        statuses = {s.value: s for s in Status}
        f.statuses = self._ask_set("Status " + "/".join(statuses), f.statuses, statuses, str.lower)
        f.currencies = self._ask_set("Currency " + "/".join(self.rules.currencies), f.currencies,
                                     {c: c for c in self.rules.currencies}, str.upper)
        f.sources = self._ask_set("Source " + "/".join(self.rules.sources), f.sources,
                                  {s: s for s in self.rules.sources}, str.lower)
        f.date_from = self._ask_value("Date from (YYYY-MM-DD)", f.date_from, date.fromisoformat)
        f.date_to = self._ask_value("Date to (YYYY-MM-DD)", f.date_to, date.fromisoformat)
        f.min_amount = self._ask_value("Min amount", f.min_amount, Decimal)
        f.max_amount = self._ask_value("Max amount", f.max_amount, Decimal)
        f.text = self._ask_value("Text search (id/description/customer)", f.text, str)
        if f.date_from and f.date_to and f.date_from > f.date_to:
            self.console.print("[yellow]'Date from' is after 'Date to': no transaction can match.[/yellow]")
        if f.min_amount is not None and f.max_amount is not None and f.min_amount > f.max_amount:
            self.console.print("[yellow]Min amount is greater than max amount: no transaction can match.[/yellow]")
        self.console.print("Active filters: " + ("; ".join(f.describe()) or "none"))

    def _ask_set(self, label: str, current: set, choices: dict, normalize) -> set:
        """Comma-separated values, re-asked until every item is a valid choice."""
        shown = ",".join(sorted(str(getattr(c, "value", c)) for c in current))
        while True:
            answer = self.ask(label, shown)
            if answer in ("", "-"):
                return set()
            items = [normalize(a.strip()) for a in answer.split(",") if a.strip()]
            invalid = [i for i in items if i not in choices]
            if not invalid:
                return {choices[i] for i in items}
            self.console.print(f"[red]Not valid: {', '.join(invalid)}. Choose from {', '.join(choices)}[/red]")

    def _ask_value(self, label: str, current, convert):
        """Single value, re-asked until it converts."""
        while True:
            answer = self.ask(label, "" if current is None else str(current))
            if answer in ("", "-"):
                return None
            try:
                return convert(answer)
            except (ValueError, InvalidOperation):
                self.console.print(f"[red]'{answer}' is not valid for '{label}'[/red]")

    def clear_filters(self) -> None:
        self.filter = TransactionFilter()
        self.console.print("[green]Filters cleared.[/green]")

    def show_detail(self, record: RecordResult | None = None) -> None:
        if record is None:
            key = self.ask("Transaction id (e.g. bank_api:BNK-1001), source id, or record ref (file#index)")
            record = self.result.find(key) if key else None
            if record is None:
                self.console.print(f"[red]No unique record found for '{key}'[/red]")
                return

        state = "[green]VALID[/green]" if record.is_valid else "[red]REJECTED[/red]"
        info = Table.grid(padding=(0, 2))
        info.add_row("Record", record.ref)
        info.add_row("Result", state)
        info.add_row("Source", f"{record.source or '-'} (detected by {record.detected_by or '-'})")

        fields = Table(title="Normalized fields", box=box.SIMPLE)
        fields.add_column("Field")
        fields.add_column("Value")
        fields.add_column("Rule applied", style="dim")
        values = record.transaction.to_dict() if record.transaction else {
            k: (v.isoformat().replace("+00:00", "Z") if hasattr(v, "isoformat") else str(v) if v is not None else None)
            for k, v in record.fields.items()
        }
        for name, value in values.items():
            fields.add_row(name, "-" if value is None else str(value), record.trace.get(name, ""))

        parts = [info, fields]
        if record.issues:
            issues = Table(title="Issues", box=box.SIMPLE)
            for col in ("Severity", "Code", "Field", "Message"):
                issues.add_column(col)
            for i in record.issues:
                color = "yellow" if i.code in WARNING_CODES else "red"
                issues.add_row(f"[{color}]{i.severity.value}[/{color}]", i.code.value, i.field or "-", i.message)
            parts.append(issues)
        raw = json.dumps(record.raw, indent=2, ensure_ascii=False)
        parts.append(Panel(Syntax(raw, "json", theme="ansi_dark", word_wrap=True), title="Raw record"))
        self.console.print(Panel(Group(*parts), title="Detail", border_style="cyan"))
        self.pause()

    def show_rejected(self) -> None:
        rejected = self.result.rejected
        code_filter = self.ask("Filter by issue code (Enter for all)").upper()
        if code_filter:
            rejected = [r for r in rejected if any(i.code.value == code_filter for i in r.issues)]
        table = Table(title=f"Rejected records ({len(rejected)})", box=box.SIMPLE_HEAVY)
        table.add_column("#", justify="right")
        table.add_column("Ref", no_wrap=True)
        table.add_column("Source")
        table.add_column("Source id")
        table.add_column("Errors")
        table.add_column("First message", overflow="fold")
        for n, r in enumerate(rejected, 1):
            errors = r.errors
            table.add_row(
                str(n), r.ref, r.source or "-", str(r.fields.get("source_id", "-")),
                ", ".join(i.code.value for i in errors), errors[0].message if errors else "",
            )
        self.console.print(table)
        while rejected:
            choice = self.ask("Record # to inspect (Enter to go back)")
            if not choice:
                return
            if choice.isdigit() and 1 <= int(choice) <= len(rejected):
                self.show_detail(rejected[int(choice) - 1])
            else:
                self.console.print(f"[red]Enter a number between 1 and {len(rejected)}[/red]")
        self.pause()

    def show_issue_report(self) -> None:
        counts = self.metrics.issues_by_code
        table = Table(title="Issues found", box=box.SIMPLE_HEAVY)
        for col in ("Code", "Severity", "Count", "Meaning", "Action taken"):
            table.add_column(col, justify="right" if col == "Count" else "left")
        for code, count in counts.items():
            issue = IssueCode(code)
            warning = issue in WARNING_CODES
            table.add_row(
                code,
                "[yellow]warning[/yellow]" if warning else "[red]error[/red]",
                str(count),
                ISSUE_DESCRIPTIONS[issue],
                "kept, flagged" if warning else "quarantined, excluded from totals",
            )
        if not counts:
            table.add_row("-", "-", "0", "No issues found", "-")
        self.console.print(table)
        self.pause()

    def show_rules(self) -> None:
        r = self.rules
        cur = Table(title="Supported currencies", box=box.SIMPLE)
        cur.add_column("Code")
        cur.add_column("Decimals", justify="right")
        cur.add_column("Aliases")
        for code, decimals in r.currencies.items():
            aliases = [a for a, c in r.currency_aliases.items() if c == code]
            cur.add_row(code, str(decimals), ", ".join(aliases) or "-")

        st = Table(title="Status mapping", box=box.SIMPLE)
        st.add_column("Canonical")
        st.add_column("Accepted raw values")
        for status in Status:
            raws = [k for k, v in r.status_map.items() if v is status]
            st.add_row(Text(status.value, style=STATUS_STYLE[status]), ", ".join(raws))

        src = Table(title="Sources", box=box.SIMPLE)
        for col in ("Source", "Detection", "Amount", "Date formats", "Timezone", "Overrides"):
            src.add_column(col, overflow="fold")
        for s in r.sources.values():
            detection = f"marker {s.marker.path}={s.marker.equals}" if s.marker else "signature only"
            overrides = []
            if s.currency_aliases:
                overrides.append("currency " + ", ".join(f"{k}->{v}" for k, v in s.currency_aliases.items()))
            if s.status_map:
                overrides.append("status " + ", ".join(f"{k}->{v.value}" for k, v in s.status_map.items()))
            src.add_row(
                f"{s.name}\n[dim]{s.label}[/dim]",
                f"{detection}\nsignature: {', '.join(s.signature)}",
                f"{s.amount_unit} units, sep '{s.decimal_separator}'",
                "\n".join(s.date_formats),
                s.timezone.key if s.timezone else f"{r.default_timezone.key} (default)",
                "\n".join(overrides) or "-",
            )
        self.console.print(Panel(Group(cur, st, src), title=f"Rules v{r.version} - {r.path}", border_style="blue"))
        self.pause()

    def export(self) -> None:
        out = self.ask("Output directory", "output")
        try:
            written = export_results(self.result, out)
        except OSError as exc:
            self.console.print(f"[red]Export failed:[/red] {exc}")
            return
        for path in written:
            self.console.print(f"[green]wrote[/green] {path}")

    def load_prompt(self) -> None:
        answer = self.ask("JSON file path(s), comma-separated")
        if answer:
            paths = [p.strip().strip('"') for p in answer.split(",") if p.strip()]
            if self.load(paths):
                self.console.print(f"[green]Loaded {len(self.result.records)} records.[/green]")


def print_summary(app: App) -> None:
    # plain print: rich would wrap/highlight lines and break the JSON for pipes
    print(json.dumps(app.metrics.to_dict(), indent=2))


def default_inputs() -> list[str]:
    return [str(p) for p in sorted(DATA_DIR.glob("*.json"))]


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure") and not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="txnorm", description="Normalize and explore multi-source transactions.")
    parser.add_argument("files", nargs="*", help="JSON input files (default: every file in data/)")
    parser.add_argument("--rules", default=None, help=f"rules file (default: {DEFAULT_RULES_PATH})")
    parser.add_argument("--summary", action="store_true", help="print metrics as JSON and exit")
    parser.add_argument("--export", metavar="DIR", help="write normalized/rejected/metrics files and exit")
    args = parser.parse_args(argv)

    console = Console()
    try:
        rules = load_rules(args.rules)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        return 2

    app = App(rules, console)
    files = args.files or default_inputs()
    if not files:
        console.print(f"[red]No input files given and none found in {DATA_DIR}[/red]")
        return 1
    if not app.load(files):
        return 1

    if args.summary or args.export:
        if args.export:
            for path in export_results(app.result, args.export):
                console.print(f"wrote {path}")
        if args.summary:
            print_summary(app)
        return 0

    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
