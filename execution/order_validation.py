"""Signed-unit order validation; LONG/SHORT internally, BUY/SELL compatibility."""
from __future__ import annotations

from math import isfinite


class OrderValidation:
    def validate(self, instrument, direction, units, sl_price, tp_price, price, min_distance):
        errors = []
        side = {"LONG": 1, "SHORT": -1, "BUY": 1, "SELL": -1}.get(str(direction).upper())
        if side is None:
            errors.append("Unknown direction")
        values = (("units", units), ("SL", sl_price), ("TP", tp_price),
                  ("price", price), ("distance", min_distance))
        for name, value in values:
            try:
                if not isfinite(float(value)):
                    errors.append(name + " not finite")
            except (TypeError, ValueError, OverflowError):
                errors.append(name + " invalid")
        if errors:
            return {"is_valid": False, "errors": errors}
        if not isinstance(instrument, str) or not instrument:
            errors.append("Invalid instrument")
        units = float(units)
        sl_price, tp_price, price, min_distance = map(
            float, (sl_price, tp_price, price, min_distance)
        )
        if not units or (side and side * units <= 0):
            errors.append("Direction/units mismatch")
        if min(sl_price, tp_price, price) <= 0 or min_distance < 0:
            errors.append("Nonpositive price or distance")
        if side == 1 and not sl_price < price < tp_price:
            errors.append("LONG SL/TP mismatch")
        if side == -1 and not tp_price < price < sl_price:
            errors.append("SHORT SL/TP mismatch")
        if min(abs(price - sl_price), abs(price - tp_price)) < min_distance:
            errors.append("SL/TP below minimum distance")
        return {"is_valid": not errors, "errors": errors}
