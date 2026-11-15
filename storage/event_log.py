"""Category 6 append-only event log: no secrets, stable schema and fail closed.

One record per event, not one record per guessed broker action. This audit
log is NOT authoritative execution state (IntentJournal and LifecycleStore are).
"""
from __future__ import annotations
import csv
from datetime import datetime
import os
from pathlib import Path
import re
from utils.time_tools import iso_utc, utc_now

FIELDS = ("timestamp","event_id","correlation_id","instrument","direction",
          "stage","event","outcome","reason_code","attempt_id","order_id",
          "transaction_id","trade_id","detail")
SAFE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
STAGES = frozenset(("SIGNAL","RISK","REQUEST","RESULT","TRANSACTION","TRADE",
                    "CLOSE","BLOCK","HEARTBEAT","MANAGEMENT"))
OUTCOMES = frozenset(("ATTEMPT","SUCCESS","FAILURE","UNKNOWN","BLOCKED","INFO"))
class AuditError(RuntimeError): pass

def _field(value, name, *, optional=True):
    if value is None and optional: return ""
    if not isinstance(value,str) or (value and not SAFE.fullmatch(value)):
        raise AuditError("Invalid audit "+name)
    if not optional and not value: raise AuditError("Missing audit "+name)
    return value

class EventLog:
    def __init__(self, directory="csv"):
        self.directory = Path(directory)
        self.path = self.directory/"events.csv"

    def emit(self, *, event_id, correlation_id, stage, event, outcome,
             reason_code, instrument=None, direction=None, attempt_id=None,
             order_id=None, transaction_id=None, trade_id=None, detail=None,
             timestamp=None):
        if stage not in STAGES or outcome not in OUTCOMES:
            raise AuditError("Invalid audit stage/outcome")
        if direction not in (None,"LONG","SHORT"): raise AuditError("Invalid direction")
        values = {
            "timestamp": iso_utc(utc_now() if timestamp is None else timestamp),
            "event_id": _field(event_id,"event_id",optional=False),
            "correlation_id": _field(correlation_id,"correlation_id",optional=False),
            "instrument": _field(instrument,"instrument"),
            "direction": direction or "",
            "stage": stage,"event": _field(event,"event",optional=False),
            "outcome": outcome,
            "reason_code": _field(reason_code,"reason_code",optional=False),
            "attempt_id": _field(attempt_id,"attempt_id"),
            "order_id": _field(order_id,"order_id"),
            "transaction_id": _field(transaction_id,"transaction_id"),
            "trade_id": _field(trade_id,"trade_id"),
            "detail": _field(detail,"detail"),
        }
        try:
            self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            # Exclusive writer: an orphaned lock blocks writes; no silent recovery.
            lock = self.path.with_suffix(".lock")
            fd = os.open(str(lock),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except OSError as exc:
            raise AuditError("Audit log unavailable/locked") from exc
        try:
            os.close(fd)
            exists=self.path.exists()
            if self.path.is_symlink(): raise AuditError("Audit symlink forbidden")
            if exists:
                with self.path.open(newline="",encoding="utf-8") as stream:
                    reader=csv.reader(stream)
                    if next(reader,None)!=list(FIELDS):
                        raise AuditError("Incompatible CSV audit schema")
            with self.path.open("a",newline="",encoding="utf-8") as stream:
                writer=csv.DictWriter(stream,fieldnames=FIELDS)
                if not exists: writer.writeheader()
                writer.writerow(values)
                stream.flush()
                os.fsync(stream.fileno())
            return values
        except (OSError,csv.Error) as exc:
            raise AuditError("Audit log append failed") from exc
        finally:
            try: lock.unlink()
            except OSError: pass
