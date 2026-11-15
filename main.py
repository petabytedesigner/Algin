"""Algin Practice entrypoint. Never requests a live OANDA endpoint."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from broker.oanda_client import OandaClient, BrokerError
from runtime.engine import TradingEngine
from storage.state_store import StateStore, StateError
from utils.event_log import EventLogger

LOG = logging.getLogger("algin")


def heartbeat(phase: str, details: str = "") -> None:
    path = Path(config.HEARTBEAT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"utc": datetime.now(timezone.utc).isoformat(),
                               "phase": phase, "details": details[:200]}), encoding="utf-8")
    tmp.replace(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Algin OANDA Practice bot")
    parser.add_argument("--once", action="store_true", help="Run exactly one cycle")
    parser.add_argument("--submit-practice", action="store_true", help="Allow Practice order mutations only when also explicitly enabled in environment")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not config.API_KEY or not config.ACCOUNT_ID:
        LOG.error("Set OANDA_API_TOKEN and OANDA_ACCOUNT_ID; do not put them in Git")
        return 2
    allowed = args.submit_practice and config.ENABLE_ORDER_SUBMISSION
    if args.submit_practice and not allowed:
        LOG.error("Practice submit requires ALGIN_PRACTICE_SUBMIT=I_ACCEPT_PRACTICE_ORDERS")
        return 2
    if not allowed:
        LOG.warning("OBSERVE ONLY: broker writes disabled")
    client = OandaClient(config.API_KEY, config.ACCOUNT_ID)
    store = StateStore(config.ACCOUNT_ID)
    events = EventLogger(config.CSV_PATH)
    with store.single_instance():
        engine = TradingEngine(client=client, store=store, events=events,
                               submit_enabled=allowed)
        while True:
            heartbeat("RUNNING")
            try:
                engine.cycle()
                heartbeat("CYCLE_COMPLETED")
            except (StateError, BrokerError, ValueError, KeyError, TypeError) as exc:
                LOG.error("Cycle blocked: %s", exc.__class__.__name__)
                events.emit("CYCLE_BLOCKED", reason=exc.__class__.__name__, status="BLOCKED")
                heartbeat("BLOCKED", exc.__class__.__name__)
                if isinstance(exc, StateError):
                    return 3
            if args.once:
                return 0
            time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (StateError, KeyboardInterrupt) as exc:
        logging.error("Stopped: %s", exc.__class__.__name__)
        sys.exit(3 if isinstance(exc, StateError) else 130)
