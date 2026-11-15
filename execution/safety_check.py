
from __future__ import annotations
from typing import Optional, Dict, Any
from utils.common import log, safe_request
import config as config
import datetime as dt
import os, csv

def _csv_enabled() -> bool:
    return getattr(config, "CSV_ENABLED", False)

def _csv_dir() -> str:
    return getattr(config, "CSV_PATH", "csv")

def _ensure_csv_dir() -> None:
    if _csv_enabled() and not os.path.exists(_csv_dir()):
        os.makedirs(_csv_dir(), exist_ok=True)

def csv_log(event: str, instrument: str = "", info: str = "") -> None:
    """CSV logging i thjeshtë për SafetyCheck.

    Këtu NUK prekim logjikën e tregtimit, vetëm ruajmë raportin e sigurisë.
    """
    if not _csv_enabled():
        return
    _ensure_csv_dir()
    path = os.path.join(_csv_dir(), f"events_{dt.datetime.utcnow().date()}.csv")
    new_file = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["time", "event", "instrument", "info"])
        w.writerow([dt.datetime.utcnow().isoformat(), event, instrument, info])


class SafetyCheck:
    """Kontrolle sigurie për tregtitë ekzistuese.

    Qëllimi:
    - Sigurohemi që çdo trade ka SL/TP aktiv dhe të vlefshëm.
    - Kontrollojmë nëse ndonjë trade është i pambrojtur.
    - Jep sinjale për rikthim të mbrojtjes (SL/TP) ose mbyllje urgjente.

    Nuk bën modifikime vetë — vetëm raporton.
    Modifikimet i bën trade_executor ose partial_close_engine.
    """

    def __init__(self):
        self.base_url = config.OANDA_URL
        self.account_id = config.ACCOUNT_ID
        self.api_key = config.API_KEY

    def _get_open_trades(self) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/accounts/{self.account_id}/openTrades"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        r = safe_request("GET", url, headers=headers)
        if not r or r.status_code != 200:
            log("⚠️ Nuk morëm dot listën e tregtive të hapura.")
            # CSV log i dështimit të marrjes së tregtive
            csv_log("SAFETY_FETCH_FAIL", "", f"status={getattr(r,'status_code',None)}")
            return None
        return r.json()

    def check_all(self) -> Dict[str, Dict[str, Any]]:
        """Kthen një raport për çdo trade të hapur dhe logon rezultatet në CSV (vetëm kur ka probleme)."""
        data = self._get_open_trades()
        if data is None:
            return {'__broker_error__': {'issues': ['Broker openTrades unavailable']}}
        if not isinstance(data.get('trades'), list):
            return {'__broker_error__': {'issues': ['Malformed broker trades']}}

        trades = data.get("trades", [])
        report: Dict[str, Dict[str, Any]] = {}

        for t in trades:
            tid = t.get("id")
            inst = t.get("instrument")

            stop_loss = t.get("stopLossOrder")
            take_profit = t.get("takeProfitOrder")

            issues = []

            if not stop_loss:
                issues.append("Mungon SL")

            if not take_profit:
                issues.append("Mungon TP")

            report[tid] = {
                "instrument": inst,
                "has_sl": bool(stop_loss),
                "has_tp": bool(take_profit),
                "issues": issues,
            }

            if issues:
                msg = f"⚠️ Trade {tid} ({inst}) ka probleme sigurie: {issues}"
                log(msg)
                csv_log("SAFETY_ISSUE", inst, f"trade_id={tid}; issues={issues}")
            else:
                log(f"✅ Trade {tid} ({inst}) është i siguruar (SL/TP OK)")

        return report
