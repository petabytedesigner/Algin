from __future__ import annotations
import config as config
from utils.common import safe_request, log
from typing import Optional, Dict, Any, List

class DataHandler:
    """Merr candles, pricing dhe të dhëna të tjera nga OANDA.
    I përdor strategy dhe main.
    """

    def __init__(self):
        self.base_url=config.OANDA_URL
        self.account_id=config.ACCOUNT_ID
        self.api_key=config.API_KEY

    def get_candles(self,instrument:str,granularity:str="M15",count:int=200)->Optional[List[Dict[str,Any]]]:
        url=f"{self.base_url}/accounts/{self.account_id}/instruments/{instrument}/candles"
        headers={"Authorization":f"Bearer {self.api_key}"}
        params={"granularity":granularity,"count":str(count),"price":"M"}
        r=safe_request("GET",url,headers=headers,params=params)
        if not r or r.status_code!=200:
            log(f"⚠️ DataHandler: dështoi candles për {instrument}")
            return None
        try:
            data=r.json().get("candles",[])
            out=[]
            for c in data:
                out.append({
                    "time":c.get("time"),
                    "open":float(c["mid"]["o"]),
                    "high":float(c["mid"]["h"]),
                    "low":float(c["mid"]["l"]),
                    "close":float(c["mid"]["c"]),
                })
            return out
        except Exception as e:
            log(f"⚠️ Error parse candles {instrument}: {e}")
            return None

    def get_price(self,instrument:str)->Optional[Dict[str,float]]:
        url=f"{self.base_url}/accounts/{self.account_id}/pricing"
        headers={"Authorization":f"Bearer {self.api_key}"}
        params={"instruments":instrument}
        r=safe_request("GET",url,headers=headers,params=params)
        if not r or r.status_code!=200:
            log(f"⚠️ DataHandler: pricing fail {instrument}")
            return None
        try:
            d=r.json()["prices"][0]
            return {
                "bid":float(d["bids"][0]["price"]),
                "ask":float(d["asks"][0]["price"]),
                "mid":(float(d["bids"][0]["price"]) + float(d["asks"][0]["price"])) / 2
            }
        except:
            return None
