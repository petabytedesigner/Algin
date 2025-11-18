from __future__ import annotations
from typing import Dict, Any
from utils.utils_v10 import log

class OrderValidation:
    """Validon orderat përpara se t'i dërgojmë te OANDA.

    Kontrollojmë:
    - SL/TP janë në drejtimin e duhur
    - SL nuk është shumë afër çmimit
    - TP nuk është shumë afër çmimit
    - units përputhen me direction (BUY → units>0, SELL → units<0)
    - çmimet janë numra të vlefshëm
    """

    def validate(
        self,
        instrument: str,
        direction: str,
        units: int,
        sl_price: float,
        tp_price: float,
        price: float,
        min_distance: float
    ) -> Dict[str, Any]:
        """Kthen:
        {
            "is_valid": True/False,
            "errors": [ ... ]
        }
        """
        errors = []

        # -------------------------
        # 1) Validim i units
        # -------------------------
        if direction == "BUY" and units <= 0:
            errors.append("Direction BUY por units <= 0.")
        if direction == "SELL" and units >= 0:
            errors.append("Direction SELL por units >= 0.")

        # -------------------------
        # 2) SL/TP duhet të jenë numra realë
        # -------------------------
        if sl_price <= 0 or tp_price <= 0:
            errors.append("SL/TP duhet të jenë numra pozitivë.")

        # -------------------------
        # 3) SL/TP duhet të jenë në drejtimin e duhur
        # -------------------------
        if direction == "BUY":
            if sl_price >= price:
                errors.append("SL për BUY duhet të jetë poshtë çmimit.")
            if tp_price <= price:
                errors.append("TP për BUY duhet të jetë mbi çmimin.")
        if direction == "SELL":
            if sl_price <= price:
                errors.append("SL për SELL duhet të jetë mbi çmimin.")
            if tp_price >= price:
                errors.append("TP për SELL duhet të jetë poshtë çmimit.")

        # -------------------------
        # 4) Distanca minimale
        # -------------------------
        if abs(price - sl_price) < min_distance:
            errors.append("SL shumë afër çmimit (risk i tepruar).")
        if abs(price - tp_price) < min_distance:
            errors.append("TP shumë afër çmimit (të padobishëm).")

        # -------------------------
        # 5) Përgatit rezultatin
        # -------------------------
        if errors:
            log(f"❌ Order Validation FAILED për {instrument}: {errors}")
            return {"is_valid": False, "errors": errors}

        return {"is_valid": True, "errors": []}
