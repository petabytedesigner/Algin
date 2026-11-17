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
        # The journal intent is fsynced BEFORE the HTTP mutation. This test
        # never blind-retries a timed out/ambiguous POST or PUT.
        self.log("WRITE_INTENT_DURABLE", operation=operation)
        self.writes += 1
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
