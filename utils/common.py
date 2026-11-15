from __future__ import annotations
import requests, time, json, datetime as dt
from typing import Optional, Dict, Any

# ---------------------------------------------
# Logging minimal, i pastër dhe i sigurt
# ---------------------------------------------
def log(msg: str) -> None:
    now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}")

# ---------------------------------------------
# Kërkesa të sigurta HTTP me retry
# ---------------------------------------------
def safe_request(
    method: str,
    url: str,
    headers: Optional[Dict[str,str]] = None,
    params: Optional[Dict[str,Any]] = None,
    data: Optional[str] = None,
    retries: int = 3,
    timeout: int = 10,
) -> Optional[requests.Response]:

    # The old convenience adapter is restricted to broker reads.
    # All account-changing requests must pass through journaled OandaClient.
    if method.upper() != "GET":
        log("Legacy HTTP adapter blocked an account-changing request.")
        return None
    for i in range(retries):
        try:
            if method.upper() == "GET":
                r = requests.get(url, headers=headers, params=params, timeout=timeout)
            elif method.upper() == "POST":
                r = requests.post(url, headers=headers, params=params, data=data, timeout=timeout)
            elif method.upper() == "PUT":
                r = requests.put(url, headers=headers, params=params, data=data, timeout=timeout)
            else:
                log(f"⚠️ safe_request: metoda {method} jo e njohur")
                return None

            return r
        except Exception as e:
            log(f"⚠️ safe_request error ({i+1}/{retries}): {e}")
            time.sleep(1.0)

    return None

# ---------------------------------------------
# Pricing i pastër (bid/ask/mid)
# ---------------------------------------------
import config as config

def get_pricing(instrument: str) -> Optional[Dict[str,float]]:
    url = f"{config.OANDA_URL}/accounts/{config.ACCOUNT_ID}/pricing"
    headers = {"Authorization": f"Bearer {config.API_KEY}"}
    params = {"instruments": instrument}

    r = safe_request("GET", url, headers=headers, params=params)
    if not r or r.status_code != 200:
        return None

    try:
        jp = r.json()
        p = jp["prices"][0]
        bid = float(p["bids"][0]["price"])
        ask = float(p["asks"][0]["price"])
        return {"bid": bid, "ask": ask, "mid": (bid+ask)/2}
    except:
        return None

# ---------------------------------------------
# Sleep minutes (përdoret nga main)
# ---------------------------------------------
def sleep_minutes(m: int) -> None:
    time.sleep(m * 60)

# ---------------------------------------------
# Timestamps të pastër
# ---------------------------------------------
def now_utc_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

# ---------------------------------------------
# Llogaritja e madhësisë së një pip-i për çdo instrument
# ---------------------------------------------
def pip_size(instrument: str) -> float:
    if instrument.endswith("JPY"):
        return 0.01
    if "XAU" in instrument:  # Gold
        return 0.10
    if "BTC" in instrument:  # Bitcoin CFD
        return 1.0
    return 0.0001
