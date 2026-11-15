"""P59/P61 safe per-cycle telemetry, without changing return-code contracts."""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
import logging
import uuid

import config
from telemetry.events import AuditEvents, AuditError, event_id
from telemetry.heartbeat import HeartbeatStore, HeartbeatError

LOG = logging.getLogger("algin")
CURRENT = ContextVar("algin_audit_cycle", default=None)


class CycleAudit:
    def __init__(self, account, cycle_id):
        self.cycle_id = cycle_id
        self.events = AuditEvents(account)

    def emit(self, kind, *, stage, outcome, reason_code="", instrument="",
             signal_id="", attempt_id="", trade_id="", direction="",
             grade="", candle_time="", **numeric):
        eid = event_id(
            self.cycle_id, kind, stage, reason_code or "NONE",
            instrument or "ALL", direction or "NONE",
            signal_id or "NONE", attempt_id or "NONE",
            trade_id or "NONE"
        )
        return self.events.emit(
            kind, eid=eid, stage=stage, outcome=outcome,
            reason_code=reason_code, cycle_id=self.cycle_id,
            signal_id=signal_id, attempt_id=attempt_id,
            trade_id=trade_id, instrument=instrument,
            direction=direction, grade=grade,
            candle_time=candle_time, **numeric
        )


def emit_block(reason, *, instrument="", kind="SIGNAL_BLOCK",
               stage="STRATEGY", direction="", candle_time=""):
    active = CURRENT.get()
    if active is not None:
        active.emit(
            kind, stage=stage, outcome="BLOCKED",
            reason_code=reason, instrument=instrument,
            direction=direction, candle_time=candle_time
        )


def observed_cycle(kind):
    """Heartbeat ALIVE prior to broker read; COMPLETE only on return 0.

    An audit/heartbeat write failure rejects the observation run. Existing
    signal and risk contracts remain read-only, never broker mutations.
    """
    def wrap(function):
        @wraps(function)
        def run(*args, **kwargs):
            if not config.ACCOUNT_ID:
                return function(*args, **kwargs)
            cycle_id = "cycle-" + uuid.uuid4().hex
            try:
                heartbeat = HeartbeatStore(config.ACCOUNT_ID)
                heartbeat.start(cycle_id, kind=kind)
            except HeartbeatError:
                LOG.error("Telemetry heartbeat unavailable: cycle blocked")
                return 3
            token = CURRENT.set(CycleAudit(config.ACCOUNT_ID, cycle_id))
            status = 3
            try:
                result = function(*args, **kwargs)
                if type(result) is not int:
                    LOG.error("Invalid observation-cycle return code")
                else:
                    status = result
            except (AuditError, HeartbeatError):
                LOG.error("Audit evidence not durable: cycle blocked")
            except Exception:
                LOG.error("Observation cycle raised an unverified error; blocked")
            finally:
                CURRENT.reset(token)
            try:
                heartbeat.finish(cycle_id, kind=kind, success=(status == 0))
            except HeartbeatError:
                LOG.error("Heartbeat could not finalize safely")
                return 3
            return status
        return run
    return wrap
