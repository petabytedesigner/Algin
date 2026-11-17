"""Account-scoped OANDA REST v20 instrument catalog, read-only.

Only the account's GET /instruments reply may attest instrument availability.
Configured symbols, cached labels and a valid pricing reply alone cannot
establish account permission. No HTTP or credentials are used here.
"""
from __future__ import annotations

import re

from broker.oanda_v20_contract import transaction_id
from risk.trade_plan import InstrumentRules, PlanRejected, number


_SYMBOL = re.compile(r"[A-Z0-9]{2,20}_[A-Z]{3}\Z", re.ASCII)


class InstrumentCatalogError(ValueError):
    """Account-specific instrument listing is incomplete/ambiguous."""


def parse_account_instruments(payload: object) -> dict[str, InstrumentRules]:
    """Validate a full account catalog and select FX instrument rule objects.

    OANDA returns non-FX instruments too; their availability is recorded by
    the broker, but this project's verified execution/weekly-market policy
    only supports FX currency pairs. Do not guess pip location or units.
    """
    if not isinstance(payload, dict) or not transaction_id(
        payload.get("lastTransactionID")
    ):
        raise InstrumentCatalogError("Unidentified account instrument snapshot")
    listed = payload.get("instruments")
    if not isinstance(listed, list) or not listed or len(listed) > 5000:
        raise InstrumentCatalogError("Missing/oversized instrument catalog")
    result = {}
    seen = set()
    for item in listed:
        if not isinstance(item, dict):
            raise InstrumentCatalogError("Malformed account instrument")
        name, kind = item.get("name"), item.get("type")
        if (not isinstance(name, str) or _SYMBOL.fullmatch(name) is None
            or name in seen or not isinstance(kind, str)):
            raise InstrumentCatalogError("Invalid or duplicate instrument identity")
        seen.add(name)
        if kind != "CURRENCY":
            continue
        # An otherwise advertised FX instrument lacking metadata is not
        # eligible; never infer pip precision, limits or margin rate.
        try:
            result[name] = InstrumentRules(
                instrument=name,
                kind=kind,
                pip_location=_integer(item["pipLocation"], "pipLocation"),
                display_precision=_integer(
                    item["displayPrecision"], "displayPrecision"
                ),
                units_precision=_integer(
                    item["tradeUnitsPrecision"], "tradeUnitsPrecision"
                ),
                minimum_trade_size=number(
                    item["minimumTradeSize"], "minimumTradeSize", positive=True
                ),
                maximum_order_units=number(
                    item["maximumOrderUnits"], "maximumOrderUnits", positive=True
                ),
                maximum_position_size=number(
                    item["maximumPositionSize"], "maximumPositionSize"
                ),
                margin_rate=number(item["marginRate"], "marginRate", positive=True),
            )
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise InstrumentCatalogError(
                "Unverified account FX instrument rules"
            ) from exc
    return result


def _integer(value, name):
    if type(value) is not int:
        raise InstrumentCatalogError(f"Invalid {name}")
    return value
