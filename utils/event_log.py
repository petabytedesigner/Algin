"""Single-schema append-only audit log; no credentials or raw broker JSON."""
from __future__ import annotations

from datetime import datetime, timezone
import csv
from pathlib import Path


FIELDS = ("time", "event", "instrument", "direction", "grade", "score",
          "signal_id", "intent_id", "trade_id", "status", "reason", "risk_pct",
          "units", "entry", "stop_loss", "take_profit")


class EventLogger:
    def __init__(self, directory="csv"):
        self.directory = Path(directory)

    def emit(self, event: str, **data):
        self.directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        # New schema in a distinct filename; never corrupt historical V10 CSV.
        file = self.directory / ("events_v2_" + now.date().isoformat() + ".csv")
        new = not file.exists()
        row = {field: "" for field in FIELDS}
        row["time"] = now.isoformat()
        row["event"] = event
        for field in FIELDS:
            if field in data:
                row[field] = str(data[field])[:512]
        with file.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            if new:
                writer.writeheader()
            writer.writerow(row)
