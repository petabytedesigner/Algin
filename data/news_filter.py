from __future__ import annotations
import config as config
from utils.common import log
from typing import Optional, List, Dict, Any
import datetime as _dt

class NewsFilter:
    """Filtri i lajmeve për robotin V10.

    Që bën:
    - Merr listën e lajmeve nga ForexFactory API ose burimi yt i integruar.
    - Filtron vetëm ato me impakt HIGH.
    - Kontrollon nëse instrumenti prek lajmin.
    - Kontrollon nëse jemi brenda dritares së ndalimit (lockout window)
      p.sh. 30 min para + 30 min pas.
    """

    def __init__(self):
        self.lockout_minutes_before = config.NEWS_LOCKOUT_MINUTES_BEFORE
        self.lockout_minutes_after = config.NEWS_LOCKOUT_MINUTES_AFTER

    # ---------------------------------------------------------
    #  Leximi i lajmeve (shumë fleksibël: përdor API-n që ke)
    # ---------------------------------------------------------
    def load_news(self) -> List[Dict[str, Any]]:
        """Ngarkon lajmet nga burimi i konfiguruar.

        Pret që config të ketë NEWS_PROVIDER_URL ose listë të ngurtë.
        Përdoret nga main çdo X minuta.

        Format i pritur:
        [
          {
            "time": "2025-11-17T13:30:00Z",
            "impact": "HIGH",
            "currency": "USD",
            "title": "CPI m/m",
          },
          ...
        ]
        """
        try:
            # Burimi mund të jetë një URL API ose një listë statike
            if hasattr(config, "NEWS_PROVIDER_URL") and config.NEWS_PROVIDER_URL:
                import requests
                r = requests.get(config.NEWS_PROVIDER_URL, timeout=10)
                if r.status_code == 200:
                    return r.json()
                else:
                    log(f"⚠️ NewsFilter: dështoi API news ({r.status_code})")
                    return []
            # Nëse nuk ka URL, përdorim një listë të ngurtë
            if hasattr(config, "STATIC_NEWS_LIST"):
                return config.STATIC_NEWS_LIST
        except Exception as e:
            log(f"⚠️ NewsFilter: error load_news: {e}")

        return []

    # ---------------------------------------------------------
    #  Filtrim i lajmeve me Impact HIGH
    # ---------------------------------------------------------
    def filter_high_impact(self, news_list: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
        high=[]
        for n in news_list:
            if n.get("impact","").upper()=="HIGH":
                high.append(n)
        return high

    # ---------------------------------------------------------
    #  Kontrollon nëse instrumenti lidhet me këtë lajm
    # ---------------------------------------------------------
    def affects_instrument(self, instrument:str, news:Dict[str,Any]) -> bool:
        """Shembull:
        EUR_USD → prek kur currency është EUR ose USD
        """
        try:
            cur=news.get("currency","").upper()
            base,quote=instrument.split("_")
            return cur in (base.upper(),quote.upper())
        except:
            return False

    # ---------------------------------------------------------
    #  Kontrollon nëse jemi brenda dritares së ndalimit
    # ---------------------------------------------------------
    def is_in_lockout(self, event_time:_dt.datetime, now:_dt.datetime) -> bool:
        before=event_time - _dt.timedelta(minutes=self.lockout_minutes_before)
        after =event_time + _dt.timedelta(minutes=self.lockout_minutes_after)
        return before <= now <= after

    # ---------------------------------------------------------
    #  Funksioni final për main
    # ---------------------------------------------------------
    def block_trading(self,instrument:str, now:_dt.datetime) -> Optional[str]:
        """Kthen arsye nëse ky instrument duhet bllokuar nga tregtia.

        Përndryshe kthen None = safe to trade.
        """
        news=self.load_news()
        if not news:
            return None

        high=self.filter_high_impact(news)
        if not high:
            return None

        for n in high:
            if not self.affects_instrument(instrument,n):
                continue
            try:
                t=_dt.datetime.fromisoformat(n["time"].replace("Z","+00:00"))
            except:
                continue
            if self.is_in_lockout(t, now):
                return f"Lajm HIGH për {n.get('currency')} ({n.get('title')})"

        return None
