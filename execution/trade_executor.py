from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, Dict, Any

import config as config
from utils.common import log, safe_request, get_pricing, pip_size

import datetime as dt
import os, csv

def _csv_enabled():
    return getattr(config, "CSV_ENABLED", False)

def _csv_dir():
    return getattr(config, "CSV_PATH", "csv")

def _ensure_csv_dir():
    if _csv_enabled() and not os.path.exists(_csv_dir()):
        os.makedirs(_csv_dir(), exist_ok=True)

def csv_log(event: str, instrument: str = "", info: str = ""):
    if not _csv_enabled():
        return
    _ensure_csv_dir()
    fn = os.path.join(_csv_dir(), f"events_{dt.datetime.utcnow().date()}.csv")
    new = not os.path.exists(fn)
    with open(fn, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time","event","instrument","info"])
        w.writerow([dt.datetime.utcnow().isoformat(), event, instrument, info])


@dataclass
class OrderRequest:
    instrument: str
    direction: str          # 'BUY' ose 'SELL'
    units: int              # units të nënshkruar (+ për BUY, - për SELL)
    sl_price: float
    tp_price: float
    client_tag: Optional[str] = None  # p.sh. 'V10_BREAK_RETEST'
    comment: Optional[str] = None     # për debug / audit


class TradeExecutor:
    """Ekzekutuesi i tregtive për V10 (shtresa OANDA).

    Ky modul:
    - NUK llogarit risk, units, SL/TP logjik – këto vijnë nga risk/strategy.
    - Merr vetëm një OrderRequest gati dhe e dërgon te Oanda.
    - Vendos MARKET order + SL + TP në një hap.
    - Logon çdo hap dhe kthen tradeID ose None.

    Në këtë version:
    - Nuk menaxhon ende partial close / trailing, ato do jenë në
      partial_close_engine.py dhe do përdorin endpoint-et e OANDA
      për të modifikuar pozicione ekzistuese.
    """

    def __init__(self) -> None:
        self.base_url = config.OANDA_URL
        self.account_id = config.ACCOUNT_ID
        self.api_key = config.API_KEY

    # ---------------------------------------------------------
    #  HAPI KRYESOR: HAP TREGTI ME SL & TP
    # ---------------------------------------------------------

    def open_market_order(self, req: OrderRequest) -> Optional[str]:
        """Dërgon një MARKET order me SL & TP.

        Kthen:
        - tradeID (str) nëse çdo gjë shkon mirë
        - None nëse ka dështim (log detalet)
        """
        if not getattr(config, "ENABLE_ORDER_SUBMISSION", False):
            log("Order submission blocked by development gate.")
            return None
        if req.units == 0:
            log(f"⚠️ Units 0 për {req.instrument}, nuk dërgojmë order.")
            return None

        # Sigurohemi që direction përputhet me units
        if req.direction == "BUY" and req.units < 0:
            log("⚠️ Direction BUY por units negative, korrigjojmë në absolut.")
            units = abs(req.units)
        elif req.direction == "SELL" and req.units > 0:
            log("⚠️ Direction SELL por units pozitive, korrigjojmë në negativ.")
            units = -abs(req.units)
        else:
            units = req.units

        side = "BUY" if units > 0 else "SELL"

        # Double-check pricing vetëm për log (opsionale)
        pricing = get_pricing(req.instrument)
        if not pricing:
            log(f"⚠️ Nuk morëm pricing për {req.instrument}, por po provojmë order.")
            entry_hint = None
        else:
            entry_hint = pricing.get("ask") if side == "BUY" else pricing.get("bid")

        url = f"{self.base_url}/accounts/{self.account_id}/orders"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        order_payload: Dict[str, Any] = {
            "order": {
                "instrument": req.instrument,
                "units": str(units),
                "type": "MARKET",
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
                "stopLossOnFill": {
                    "price": f"{req.sl_price:.5f}",
                },
                "takeProfitOnFill": {
                    "price": f"{req.tp_price:.5f}",
                },
            }
        }

        # Client extensions (tag/koment) për identifikim të pozicioneve të V10
        client_ext: Dict[str, Any] = {}
        if req.client_tag:
            client_ext["tag"] = req.client_tag
        if req.comment:
            client_ext["comment"] = req.comment

        if client_ext:
            order_payload["order"]["clientExtensions"] = client_ext

        log(
            f"📤 Dërgojmë MARKET order për {req.instrument} {side} {units} "
            f"SL={req.sl_price:.5f} TP={req.tp_price:.5f}..."
        )

        r = safe_request("POST", url, headers=headers, data=json.dumps(order_payload))
        if not r:
            log(f"❌ Dështoi kërkesa e order-it për {req.instrument}: no-response")
            return None

        if r.status_code not in (200, 201):
            try:
                err = r.json()
            except Exception:
                err = r.text
            log(f"❌ OANDA order error ({r.status_code}) për {req.instrument}: {err}")
            return None

        data = r.json()
        trade_id: Optional[str] = None

        # Kërkojmë tradeOpened / filling nga përgjigja
        try:
            # OANDA kthen orderFillTransaction me tradeOpened ose tradesOpened
            tx = data.get("orderFillTransaction") or data.get("orderCreateTransaction")
            if tx:
                if "tradeOpened" in tx:
                    trade_id = tx["tradeOpened"]["tradeID"]
                
        except Exception as e:
            log(f"⚠️ Nuk arritëm të nxjerrim tradeID nga përgjigja: {e}")

        if not trade_id:
            log(f"⚠️ Order u krye por nuk gjetëm tradeID të qartë: {data}")
        else:
            log(
                f"✅ Order hapur: {req.instrument} {side} {units} "
                f"SL={req.sl_price:.5f} TP={req.tp_price:.5f} trade_id={trade_id}"
            )

        return trade_id

    # ---------------------------------------------------------
    #  FUNKSIONE NDËRTORE (do përdoren nga module të tjera)
    # ---------------------------------------------------------

    def modify_sl_tp(
        self,
        trade_id: str,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
    ) -> bool:
        """Modifikon SL/TP për një trade ekzistues.

        Përdoret nga:
        - partial_close_engine (për BE / trailing)
        - safety_check / order_validation nëse duam të përditësojmë mbrojtjen.
        """
        if not getattr(config, "ENABLE_ORDER_SUBMISSION", False):
            return False
        if sl_price is None and tp_price is None:
            return True  # asgjë për të bërë

        url = f"{self.base_url}/accounts/{self.account_id}/trades/{trade_id}/orders"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        req: Dict[str, Any] = {}
        if sl_price is not None:
            req["stopLoss"] = {"price": f"{sl_price:.5f}"}
        if tp_price is not None:
            req["takeProfit"] = {"price": f"{tp_price:.5f}"}

        if not req:
            return True

        r = safe_request("PUT", url, headers=headers, data=json.dumps(req))
        if not r or r.status_code not in (200, 201):
            log(f"⚠️ Dështoi modifikimi i SL/TP për trade {trade_id}: {r.text if r else 'no-response'}")
            return False

        log(
            f"🔁 U përditësua SL/TP për trade {trade_id} "
            f"(SL={sl_price if sl_price is not None else '-'}, "
            f"TP={tp_price if tp_price is not None else '-'})"
        )
        return True

    def close_trade(self, trade_id: str, units: Optional[int] = None) -> bool:
        """Mbyll ose mbyll pjesërisht një trade.

        Nëse units është None → mbyllim gjithë tregtinë.
        Nëse units > 0 → mbyllim aq units (p.sh. për partial close 50%).
        """
        if not getattr(config, "ENABLE_ORDER_SUBMISSION", False):
            return False
        url = f"{self.base_url}/accounts/{self.account_id}/trades/{trade_id}/close"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload: Dict[str, Any] = {}
        if units is not None:
            payload["units"] = str(units)

        r = safe_request("PUT", url, headers=headers, data=json.dumps(payload) if payload else None)
        if not r or r.status_code not in (200, 201):
            log(f"⚠️ Dështoi mbyllja e trade {trade_id}: {r.text if r else 'no-response'}")
            csv_log("TRADE_CLOSE_FAIL", "", f"trade_id={trade_id}")
            return False

        log(f"✅ Trade {trade_id} u mbyll (partial ose total).")

        # Për logim institucional: dallojmë mbylljen totale vs partial
        event = "MANUAL_CLOSE" if units is None else "SYSTEM_CLOSE"
        info = f"trade_id={trade_id}; units={units if units is not None else 'ALL'}"
        csv_log(event, "", info)

        return True
