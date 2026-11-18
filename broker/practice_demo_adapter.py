"""Isolated, hard-capped OANDA Practice demo HTTP adapter. NOT production."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import json
import os
import re

import requests
import config
from broker.practice_transport import (
    PRACTICE_API, _PRACTICE_WRITES_RELEASED, PracticeTransport, BrokerReply
)
from risk.trade_plan import number

ACCOUNT = "101-001-37674492-003"
ATTEMPT = "algin-c12-20261117-once"
EVIDENCE = Path(".local/category12_one_shot.jsonl")
ARM = "EXPLICIT_C12_DEMO_ONLY_ONE_SHOT"


class DemoBlocked(RuntimeError):
    pass


class DemoAdapter:
    """Only explicit POST order and PUT trade-management requests, once each."""
    def __init__(self, *, session=None, reader=None, evidence=EVIDENCE):
        self.http = session or requests.Session()
        self.http.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        self.reader = reader or PracticeTransport()
        self.evidence = Path(evidence)
        self.writes = 0
        self._open_payload = None
        self._trade_id = None
        self._stop = None
        self._target = None
        self._be_done = False
        self._partial_done = False
        self._halted = False

    def log(self, status, **fields):
        info = {"status": status, "account_suffix": "003", **fields}
        self.evidence.parent.mkdir(parents=True, exist_ok=True)
        with self.evidence.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(info, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps(info, sort_keys=True), flush=True)

    @staticmethod
    def gates():
        if (config.OANDA_URL != PRACTICE_API
            or config.ACCOUNT_ID != ACCOUNT
            or not config.API_KEY
            or config.ORDER_WRITES_ENABLED is not False
            or _PRACTICE_WRITES_RELEASED is not False):
            raise DemoBlocked("DEMO_ONLY_GATES_FAILED")

    def get(self, path, *, params=None):
        self.gates()
        if (not isinstance(path, str)
            or re.fullmatch(r"(?:/[A-Za-z0-9_.@-]+)*", path) is None):
            raise DemoBlocked("INVALID_GET_PATH")
        try:
            result = self.reader.read(path, params=params)
        except Exception:
            raise DemoBlocked("GET_UNAVAILABLE") from None
        if (not isinstance(result, BrokerReply) or result.status_code != 200
            or not isinstance(result.body, dict)):
            raise DemoBlocked("GET_UNVERIFIED")
        return result.body

    def transaction(self, tid, kind):
        if not isinstance(tid, str) or not tid.isdecimal():
            raise DemoBlocked("INVALID_TRANSACTION_ID")
        tx = self.get("/transactions/" + tid).get("transaction")
        if (not isinstance(tx, dict) or tx.get("id") != tid
            or tx.get("type") != kind or tx.get("accountID") != ACCOUNT):
            raise DemoBlocked("BROKER_TRANSACTION_UNVERIFIED")
        return tx

    def authorize_one_shot(self, payload):
        """Reserve exactly one bounded demo run before any mutation is possible.

        O_EXCL prevents a second process or a restarted runner from arming the
        same attempt. The evidence file is deliberately never removed here.
        """
        self.gates()
        if (self._open_payload is not None or self.writes != 0
            or os.environ.get("ALGIN_PRACTICE_ONE_SHOT_CONFIRM") != ARM
            or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"):
            raise DemoBlocked("NO_FRESH_SINGLE_RUN_AUTHORIZATION")
        if not isinstance(payload, dict) or set(payload) != {"order"}:
            raise DemoBlocked("DEMO_ORDER_SHAPE_BLOCKED")
        order = payload["order"]
        if (not isinstance(order, dict) or set(order) != {
            "instrument", "units", "type", "timeInForce", "positionFill",
            "priceBound", "stopLossOnFill", "takeProfitOnFill",
            "clientExtensions", "tradeClientExtensions",
        } or order["instrument"] != "EUR_USD" or order["units"] != "20"
            or order["type"] != "MARKET" or order["timeInForce"] != "FOK"
            or order["positionFill"] != "OPEN_ONLY"):
            raise DemoBlocked("DEMO_ORDER_SHAPE_BLOCKED")
        identity = {"id": ATTEMPT, "tag": "algin", "comment": "c12_demo"}
        if (order["clientExtensions"] != identity
            or order["tradeClientExtensions"] != identity
            or not isinstance(order["stopLossOnFill"], dict)
            or not isinstance(order["takeProfitOnFill"], dict)
            or set(order["stopLossOnFill"]) != {"price"}
            or set(order["takeProfitOnFill"]) != {"price"}):
            raise DemoBlocked("DEMO_ORDER_SHAPE_BLOCKED")
        try:
            bound = number(order["priceBound"], "price bound", positive=True)
            stop = number(order["stopLossOnFill"]["price"], "SL", positive=True)
            target = number(order["takeProfitOnFill"]["price"], "TP", positive=True)
            encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
        except (ValueError, TypeError):
            raise DemoBlocked("DEMO_ORDER_PRICE_BLOCKED") from None
        if (not stop < bound < target
            or (bound - stop) * Decimal("20") > Decimal("0.25")):
            raise DemoBlocked("DEMO_ORDER_RISK_BLOCKED")

        # Direct callers of authorize_one_shot must not skip the two most
        # important broker-side preflight conditions from the runner.
        try:
            existing = self.reader.read("/orders/@" + ATTEMPT)
        except Exception:
            raise DemoBlocked("ORDER_DEDUPLICATION_GET_FAILED") from None
        if (not isinstance(existing, BrokerReply)
            or existing.status_code != 404
            or not isinstance(existing.body, dict)
            or "order" not in str(existing.body.get("errorMessage", "")).lower()):
            raise DemoBlocked("ONE_SHOT_ORDER_ABSENCE_NOT_PROVEN")
        account = self.get("").get("account")
        if (not isinstance(account, dict) or account.get("id") != ACCOUNT
            or account.get("currency") != "USD"
            or account.get("trades") != [] or account.get("orders") != []
            or account.get("openTradeCount") != 0
            or account.get("pendingOrderCount") != 0):
            raise DemoBlocked("ONE_SHOT_ACCOUNT_NOT_FLAT")
        try:
            nav = number(account.get("NAV"), "NAV", positive=True)
            margin = number(account.get("marginAvailable"), "margin")
        except ValueError:
            raise DemoBlocked("ONE_SHOT_FUNDS_UNVERIFIED") from None
        if nav < Decimal("1000") or margin < Decimal("10"):
            raise DemoBlocked("ONE_SHOT_FUNDS_UNVERIFIED")

        self.evidence.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            raise DemoBlocked("ONE_SHOT_EVIDENCE_ALREADY_RESERVED") from None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "status": "ONE_SHOT_AUTHORIZED", "account_suffix": "003",
                    "attempt": ATTEMPT,
                }, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            directory = os.open(self.evidence.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            raise DemoBlocked("ONE_SHOT_EVIDENCE_NOT_DURABLE") from None
        self._open_payload = encoded
        self._stop, self._target = stop, target

    def _check_mutation(self, method, path, body, operation):
        if (self._open_payload is None or self._halted
            or os.environ.get("ALGIN_PRACTICE_ONE_SHOT_CONFIRM") != ARM
            or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"
            or not self.evidence.is_file()):
            raise DemoBlocked("DIRECT_OR_REPEATED_WRITE_BLOCKED")
        if operation == "OPEN":
            if ((method, path) != ("POST", "/orders") or self.writes != 0
                or json.dumps(body, sort_keys=True, allow_nan=False)
                    != self._open_payload):
                raise DemoBlocked("UNAUTHORIZED_OPEN_MUTATION")
            return
        if (self._trade_id is None or self.writes == 0
            or method != "PUT" or path not in (
                "/trades/" + self._trade_id + "/orders",
                "/trades/" + self._trade_id + "/close",
            )):
            raise DemoBlocked("UNAUTHORIZED_TRADE_MUTATION")
        if operation == "BREAK_EVEN":
            if (path != "/trades/" + self._trade_id + "/orders"
                or self._be_done or self._partial_done
                or not isinstance(body.get("stopLoss"), dict)
                or set(body) != {"stopLoss"}
                or set(body["stopLoss"]) != {"price"}):
                raise DemoBlocked("UNAUTHORIZED_BREAK_EVEN")
        elif operation == "PARTIAL_CLOSE":
            if (path != "/trades/" + self._trade_id + "/close"
                or self._partial_done or body != {"units": "10"}):
                raise DemoBlocked("UNAUTHORIZED_PARTIAL_CLOSE")
        elif operation == "FULL_CLOSE":
            if (path != "/trades/" + self._trade_id + "/close"
                or not self._partial_done or body != {"units": "ALL"}):
                raise DemoBlocked("UNAUTHORIZED_FULL_CLOSE")
        else:
            raise DemoBlocked("UNAUTHORIZED_DEMO_OPERATION")
        expected_units = Decimal("10") if operation == "FULL_CLOSE" else Decimal("20")
        trade = self.owned_trade(
            self._trade_id, expected_units, self._stop, self._target
        )
        if operation == "BREAK_EVEN":
            try:
                entry = number(trade.get("price"), "entry", positive=True)
                requested = number(body["stopLoss"]["price"], "BE", positive=True)
            except ValueError:
                raise DemoBlocked("UNAUTHORIZED_BREAK_EVEN") from None
            if requested != entry or entry <= self._stop:
                raise DemoBlocked("UNAUTHORIZED_BREAK_EVEN")

    def send(self, method, path, body, *, operation):
        self.gates()
        if self.writes >= 4 or type(body) is not dict:
            raise DemoBlocked("ONE_SHOT_WRITE_LIMIT")
        if (method, path) != ("POST", "/orders") and not (
            method == "PUT" and re.fullmatch(
                r"/trades/[0-9]{1,50}/(orders|close)", path
            )
        ):
            raise DemoBlocked("MUTATION_ENDPOINT_NOT_ALLOWED")
        self._check_mutation(method, path, body, operation)
        # The journal intent is fsynced BEFORE the HTTP mutation. This test
        # never blind-retries a timed out/ambiguous POST or PUT.
        self.log("WRITE_INTENT_DURABLE", operation=operation)
        self.writes += 1
        self._halted = True  # Remains latched for any ambiguous/rejected reply.
        try:
            result = self.http.request(
                method, PRACTICE_API + "/accounts/" + ACCOUNT + path,
                headers={"Authorization": "Bearer " + config.API_KEY,
                         "Content-Type": "application/json"},
                json=body, timeout=8, allow_redirects=False,
            )
            data = result.json()
            if not isinstance(data, dict):
                raise ValueError("Unusable broker body")
            reply = BrokerReply(
                result.status_code, data,
                request_id=result.headers.get("RequestID")
            )
        except Exception:
            self.log("UNKNOWN_NO_RETRY", operation=operation)
            raise DemoBlocked("WRITE_OUTCOME_UNKNOWN_NO_RETRY") from None
        self.log(
            "WRITE_RESULT", operation=operation, http=reply.status_code,
            has_fill=isinstance(data.get("orderFillTransaction"), dict),
        )
        if operation == "OPEN" and reply.status_code == 201:
            fill = data.get("orderFillTransaction")
            opened = fill.get("tradeOpened") if isinstance(fill, dict) else None
            if isinstance(opened, dict):
                tid = opened.get("tradeID")
                if (isinstance(tid, str) and re.fullmatch(r"[0-9]{1,50}", tid)
                    and number(opened.get("units"), "opened units") == Decimal("20")):
                    self._trade_id = tid
                    self._halted = False
        elif operation == "BREAK_EVEN" and reply.status_code == 200:
            tx = data.get("stopLossOrderTransaction")
            if (isinstance(tx, dict) and tx.get("type") == "STOP_LOSS_ORDER"
                and tx.get("tradeID") == self._trade_id):
                self._stop = number(body["stopLoss"]["price"], "BE")
                self._be_done = True
                self._halted = False
        elif operation == "PARTIAL_CLOSE" and reply.status_code == 200:
            fill = data.get("orderFillTransaction")
            reduced = fill.get("tradeReduced") if isinstance(fill, dict) else None
            if (isinstance(reduced, dict)
                and reduced.get("tradeID") == self._trade_id
                and number(reduced.get("units"), "reduced units") == Decimal("-10")):
                self._partial_done = True
                self._halted = False
        return reply

    def owned_trade(self, trade_id, units, stop, target):
        doc = self.get("/trades/" + trade_id).get("trade")
        if (not isinstance(doc, dict)
            or doc.get("id") != trade_id
            or doc.get("instrument") != "EUR_USD"
            or doc.get("state") != "OPEN"
            or doc.get("clientExtensions", {}).get("id") != ATTEMPT
            or number(doc.get("currentUnits"), "units") != units
            or number(doc.get("stopLossOrder", {}).get("price"), "SL") != stop
            or number(doc.get("takeProfitOrder", {}).get("price"), "TP") != target):
            raise DemoBlocked("OWNERSHIP_OR_PROTECTION_UNVERIFIED")
        return doc
