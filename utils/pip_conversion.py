from __future__ import annotations
from typing import Dict

# ------------------------------------------------------
#  pip_conversion.py
#
#  Kthen diferencën e çmimit në pips dhe anasjelltas.
#  Super e rëndësishme për SL/TP dhe risk-engine.
# ------------------------------------------------------

class PipConverter:

    def pip_value(self, instrument: str) -> float:
        """Madhësia e një pip-i sipas llojit të instrumentit."""
        if instrument.endswith("JPY"):
            return 0.01
        if "XAU" in instrument:  # Gold
            return 0.10
        if "BTC" in instrument:
            return 1.0
        return 0.0001

    def price_to_pips(self, instrument: str, price_diff: float) -> float:
        """Kthen ndryshimin e çmimit në pips."""
        size = self.pip_value(instrument)
        if size <= 0:
            return 0.0
        return price_diff / size

    def pips_to_price(self, instrument: str, pips: float) -> float:
        """Kthen pips → çmim sipas instrumentit."""
        size = self.pip_value(instrument)
        return pips * size
