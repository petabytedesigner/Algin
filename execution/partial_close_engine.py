
from utils.common import log
import config as config
import datetime as dt
import csv, os

def _csv_enabled():
    return getattr(config, "CSV_ENABLED", False)

def _csv_dir():
    return getattr(config, "CSV_PATH", "csv")

def _ensure_csv_dir():
    if _csv_enabled() and not os.path.exists(_csv_dir()):
        os.makedirs(_csv_dir(), exist_ok=True)

def csv_log(event, instrument="", direction="", extra=""):
    if not _csv_enabled():
        return
    _ensure_csv_dir()
    fn = os.path.join(_csv_dir(), f"events_{dt.datetime.utcnow().date()}.csv")
    new = not os.path.exists(fn)
    with open(fn, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time","event","instrument","direction","extra"])
        w.writerow([dt.datetime.utcnow().isoformat(), event, instrument, direction, extra])


class PartialCloseEngine:
    """Menaxhon Break-Even dhe Partial Close për tregtitë e hapura.

    - BE: kur rr >= 1R, SL zhvendoset në entry.
    - Partial: kur rr >= 2R, mbyllim ~50% të units.
    """

    def __init__(self, executor):
        self.executor = executor  # pritet TradeExecutor

    def check_and_execute(self):
        raise RuntimeError("Legacy partial-close policy disabled until Category 4")
        open_trades = self.executor.get_open_trades()
        if not open_trades:
            return

        for trade in open_trades:
            trade_id = trade.get("id")
            inst = trade.get("instrument")
            direction = trade.get("direction")
            entry = float(trade.get("price"))
            sl = float(trade.get("sl"))
            units = float(trade.get("units"))

            price_info = self.executor.get_price(inst)
            if not price_info:
                continue
            current = float(price_info["mid"])

            if sl == entry:
                # tashmë në BE, kalojmë direkt në kontrollin e partial
                rr = abs(current - entry) / max(abs(entry - sl), 1e-8)
            else:
                rr = abs(current - entry) / max(abs(entry - sl), 1e-8)

            # BREAK EVEN
            if rr >= 1.0 and sl != entry:
                new_sl = entry
                log(f"🔵 BE aktivizuar për {inst} | trade {trade_id}")
                csv_log("BREAK_EVEN", inst, direction, f"trade_id={trade_id}")
                self.executor.modify_sl_tp(trade_id, sl_price=new_sl, tp_price=None)

            # PARTIAL CLOSE (50%)
            if rr >= 2.0:
                half = int(units * 0.5)
                if half > 0:
                    log(f"🟠 Partial close për {inst} | {half} units | trade {trade_id}")
                    csv_log("PARTIAL_CLOSE", inst, direction, f"half_units={half}")
                    # përdorim close_trade ekzistues me units (partial)
                    self.executor.close_trade(trade_id, units=half)
