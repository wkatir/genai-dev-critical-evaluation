"""Write processing results to disk (JSON + CSV)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .metrics import compute_metrics
from .pipeline import ProcessingResult

CSV_COLUMNS = ["id", "source", "source_id", "amount", "currency", "status", "occurred_at", "description", "customer_id"]


def export_results(result: ProcessingResult, out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = [t.to_dict() for t in result.transactions]

    files = {
        "normalized_transactions.json": rows,
        "rejected_records.json": [r.to_dict() for r in result.rejected],
        "metrics.json": compute_metrics(result).to_dict(),
    }
    written = []
    for name, payload in files.items():
        path = out / name
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(path)

    csv_path = out / "normalized_transactions.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    written.append(csv_path)
    return written
