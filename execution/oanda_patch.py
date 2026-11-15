from __future__ import annotations

"""oanda_patch

Shtresë abstraksioni për thirrjet kryesore te OANDA, e përditësuar
për V10 në mënyrë që:

- të përdorë vetëm endpoint-et aktuale të OANDA v20 (pricing, orders, trades, account);
- të centralizojë logjikën e leximit të përgjigjeve (p.sh. tradeID, errors);
- të jetë i lehtë për t'u përditësuar nëse OANDA bën ndryshime në të ardhmen.

Ideja: pjesa tjetër e robotit (utils_v10, trade_executor, data_handler)
mund të përdorë këtu funksione më të larta pa u shqetësuar për formatin e saktë
të JSON-it të OANDA.
"""

from typing import Any, Dict, List, Optional

import config as config
from utils.common import log, safe_request


# -------------------------------------------------------------
#  Funksione të përgjithshme të account-it
# -------------------------------------------------------------

def get_account_summary() -> Optional[Dict[str, Any]]:
    """Kthen summary të llogarisë nga OANDA.

    /accounts/{ACCOUNT_ID}/summary
    """
    url = f"{config.OANDA_URL}/accounts/{config.ACCOUNT_ID}/summary"
    headers = {"Authorization": f"Bearer {config.API_KEY}"}
    r = safe_request("GET", url, headers=headers)
    if not r or r.status_code != 200:
        log(f"⚠️ Dështoi get_account_summary: {r.status_code if r else 'no-response'}")
        return None
    try:
        return r.json()
    except Exception as e:
        log(f"⚠️ Error në parsimin e summary JSON: {e}")
        return None


def get_open_trades_raw() -> Optional[Dict[str, Any]]:
    """Wrapper mbi /openTrades.

    Kthehet JSON-i i plotë, pa u transformuar.
    Përdoret nga safety_check, partial_close, etj.
    """
    url = f"{config.OANDA_URL}/accounts/{config.ACCOUNT_ID}/openTrades"
    headers = {"Authorization": f"Bearer {config.API_KEY}"}
    r = safe_request("GET", url, headers=headers)
    if not r or r.status_code != 200:
        log(f"⚠️ Dështoi get_open_trades_raw: {r.status_code if r else 'no-response'}")
        return None
    try:
        return r.json()
    except Exception as e:
        log(f"⚠️ Error në parsimin e openTrades JSON: {e}")
        return None


def get_open_positions_raw() -> Optional[Dict[str, Any]]:
    """Wrapper mbi /openPositions."""
    url = f"{config.OANDA_URL}/accounts/{config.ACCOUNT_ID}/openPositions"
    headers = {"Authorization": f"Bearer {config.API_KEY}"}
    r = safe_request("GET", url, headers=headers)
    if not r or r.status_code != 200:
        log(f"⚠️ Dështoi get_open_positions_raw: {r.status_code if r else 'no-response'}")
        return None
    try:
        return r.json()
    except Exception as e:
        log(f"⚠️ Error në parsimin e openPositions JSON: {e}")
        return None


# -------------------------------------------------------------
#  Normalizim i përgjigjeve të orderave / trades
# -------------------------------------------------------------

def extract_trade_id_from_order_response(data: Dict[str, Any]) -> Optional[str]:
    """Përpiqet të nxjerrë tradeID nga një përgjigje order-i OANDA.

    OANDA mund të kthejë:
    - orderFillTransaction.tradeOpened.tradeID
    - orderFillTransaction.tradesOpened[0].tradeID
    - orderFillTransaction.id (si fallback)
    Kemi këtë funksion që ta centralizojmë logjikën.
    """
    try:
        tx = data.get("orderFillTransaction") or data.get("orderCreateTransaction")
        if not tx:
            return None

        if "tradeOpened" in tx and tx["tradeOpened"]:
            return tx["tradeOpened"].get("tradeID")

        return None
    except Exception as e:
        log(f"⚠️ extract_trade_id_from_order_response exception: {e}")
        return None


def extract_error_message(data: Dict[str, Any]) -> str:
    """Nxjerr mesazhin e gabimit nga një përgjigje error-i të OANDA.

    Përdor fusha si:
    - errorMessage
    - errorMessageInternal
    - message
    si edhe fallback në serializim të JSON-it.
    """
    if not isinstance(data, dict):
        return str(data)

    for key in ("errorMessage", "errorMessageInternal", "message"):
        if key in data and data[key]:
            return str(data[key])

    # Nëse nuk gjejmë gjë të qartë
    try:
        import json
        return json.dumps(data)
    except Exception:
        return str(data)


# -------------------------------------------------------------
#  Funksione të thjeshta për pricing / candles
# -------------------------------------------------------------

def get_pricing_raw(instrument: str) -> Optional[Dict[str, Any]]:
    """Merr pricing raw nga OANDA, pa u shndërruar në bid/ask/mid.

    Përdoret kur duam të verifikojmë strukturën e JSON-it ose të
    bëjmë analiza më të avancuara.
    """
    url = f"{config.OANDA_URL}/accounts/{config.ACCOUNT_ID}/pricing"
    headers = {"Authorization": f"Bearer {config.API_KEY}"}
    params = {"instruments": instrument}
    r = safe_request("GET", url, headers=headers, params=params)
    if not r or r.status_code != 200:
        log(f"⚠️ Dështoi get_pricing_raw për {instrument}: {r.status_code if r else 'no-response'}")
        return None
    try:
        return r.json()
    except Exception as e:
        log(f"⚠️ Error në parsimin e pricing JSON për {instrument}: {e}")
        return None


def get_candles_raw(
    instrument: str,
    granularity: str = "M15",
    count: int = 100,
    price_type: str = "M",
) -> Optional[Dict[str, Any]]:
    """Merr qirinj (candles) nga OANDA në formatin raw.

    - granularity: M1, M5, M15, H1, H4, D, etj.
    - price_type: M (mid), B (bid), A (ask)
    """
    url = f"{config.OANDA_URL}/accounts/{config.ACCOUNT_ID}/instruments/{instrument}/candles"
    headers = {"Authorization": f"Bearer {config.API_KEY}"}
    params = {
        "granularity": granularity,
        "count": str(count),
        "price": price_type,
    }
    r = safe_request("GET", url, headers=headers, params=params)
    if not r or r.status_code != 200:
        log(
            f"⚠️ Dështoi get_candles_raw për {instrument} "
            f"({granularity}): {r.status_code if r else 'no-response'}"
        )
        return None
    try:
        return r.json()
    except Exception as e:
        log(f"⚠️ Error në parsimin e candles JSON për {instrument}: {e}")
        return None
