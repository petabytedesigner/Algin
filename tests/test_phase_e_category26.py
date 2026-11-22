"""Category 26: bounded broker GET recovery and fail-closed fault injection.

All broker traffic is synthetic. Production Practice writes remain disabled.
"""
from __future__ import annotations

from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

import requests

import config
from broker.account_reconciler import AccountReconciler, ReconcileError
from broker.practice_transport import (
    BrokerCommunicationError, PracticeTransport,
)
from storage.lifecycle_state import ProcessLease, StateError
from utils.common import safe_request


class Reply:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.body = body if body is not None else {"ok": True}
        self.headers = headers or {}

    def json(self):
        return self.body


class Session:
    def __init__(self, *events):
        self.events = list(events)
        self.calls = []

    def mount(self, prefix, adapter):
        self.adapter = adapter

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event


class Category26ReadTests(unittest.TestCase):
    def make_transport(self, session):
        with (patch.object(config, "API_KEY", "synthetic-token"),
              patch.object(config, "ACCOUNT_ID", "acct-1")):
            return PracticeTransport(session=session)

    def get(self, transport):
        with (patch.object(config, "API_KEY", "synthetic-token"),
              patch.object(config, "ACCOUNT_ID", "acct-1")):
            return transport.read("")

    def test_timeout_and_503_retry_get_only_with_bounded_backoff(self):
        session = Session(requests.Timeout("secret"), Reply(503), Reply())
        transport = self.make_transport(session)
        with patch("broker.practice_transport.time.sleep") as sleep:
            reply = self.get(transport)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.25, 0.5])
        self.assertTrue(all(c[0][0] == "GET" for c in session.calls))

    def test_429_retry_after_honored_when_short(self):
        session = Session(Reply(429, headers={"Retry-After": "1"}), Reply())
        transport = self.make_transport(session)
        with patch("broker.practice_transport.time.sleep") as sleep:
            self.assertEqual(self.get(transport).status_code, 200)
        sleep.assert_called_once_with(1.0)

    def test_long_retry_after_is_not_ignored(self):
        session = Session(Reply(429, headers={"Retry-After": "120"}))
        transport = self.make_transport(session)
        with (patch("broker.practice_transport.time.sleep") as sleep,
              patch("broker.practice_transport.time.monotonic", return_value=100)):
            self.assertEqual(self.get(transport).status_code, 429)
        sleep.assert_not_called()
        self.assertEqual(len(session.calls), 1)
        with patch("broker.practice_transport.time.monotonic", return_value=111):
            with self.assertRaises(BrokerCommunicationError):
                self.get(transport)

    def test_malformed_200_and_auth_errors_fail_closed_without_retry(self):
        malformed = Session(Reply(200, body=[]))
        transport = self.make_transport(malformed)
        with patch("broker.practice_transport.time.monotonic", return_value=100):
            self.assertIsNone(self.get(transport).body)
            with self.assertRaises(BrokerCommunicationError):
                self.get(transport)
        self.assertTrue(transport.read_reconciliation_required)
        self.assertEqual(len(malformed.calls), 1)

        unauthorized = Session(Reply(401, body={"error": "unauthorized"}))
        transport = self.make_transport(unauthorized)
        self.assertEqual(self.get(transport).status_code, 401)
        self.assertEqual(len(unauthorized.calls), 1)

    def test_read_circuit_requires_cooldown_and_verified_reconciliation(self):
        session = Session(Reply(503), Reply(503), Reply(503), Reply())
        transport = self.make_transport(session)
        with (patch("broker.practice_transport.time.sleep"),
              patch("broker.practice_transport.time.monotonic", return_value=100)):
            self.assertEqual(self.get(transport).status_code, 503)
            with self.assertRaises(BrokerCommunicationError):
                self.get(transport)
        self.assertEqual(len(session.calls), 3)
        with patch("broker.practice_transport.time.monotonic", return_value=111):
            self.assertEqual(self.get(transport).status_code, 200)
        self.assertTrue(transport.read_reconciliation_required)
        transport.mark_reconciled()
        self.assertFalse(transport.read_reconciliation_required)

    def test_mutating_post_never_retries_even_if_gate_patched_in_test(self):
        session = Session(requests.Timeout("unknown POST outcome"))
        transport = self.make_transport(session)
        with (patch("broker.practice_transport._PRACTICE_WRITES_RELEASED", True),
              patch.object(config, "ORDER_WRITES_ENABLED", True),
              patch.object(config, "API_KEY", "synthetic-token"),
              patch.object(config, "ACCOUNT_ID", "acct-1"),
              patch("broker.practice_transport.time.sleep") as sleep):
            with self.assertRaises(BrokerCommunicationError):
                transport.submit_market_order({"order": {"type": "MARKET"}})
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][0][0], "POST")
        sleep.assert_not_called()

    def test_legacy_get_retries_429_503_not_mutations(self):
        with (patch("utils.common.requests.get", side_effect=[Reply(429), Reply(503), Reply()]) as get,
              patch("utils.common.time.sleep") as sleep):
            self.assertEqual(safe_request("GET", "https://example.invalid", retries=3).status_code, 200)
        self.assertEqual(get.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.25, 0.5])
        with self.assertRaises(RuntimeError):
            safe_request("POST", "https://example.invalid")


class Category26RecoveryTests(unittest.TestCase):
    def test_invalid_broker_snapshot_does_not_reset_circuit_or_reconcile(self):
        from tests.test_category4 import RecoveryTransport
        with TemporaryDirectory() as root:
            class RecordingTransport(RecoveryTransport):
                read_reconciliation_required = True
                verified = False

                def mark_reconciled(self):
                    self.verified = True
                    self.read_reconciliation_required = False

            transport = RecordingTransport()
            transport.cursor = "not-a-cursor"
            service = AccountReconciler("acct-1", transport=transport, directory=root)
            with service:
                with self.assertRaises(ReconcileError):
                    service.run()
            self.assertTrue(transport.read_reconciliation_required)
            self.assertFalse(transport.verified)
            self.assertIsNone(service.store.load())

            transport.cursor = "100"
            with service:
                result = service.run()
            self.assertFalse(result.blocked_reasons)
            self.assertTrue(transport.verified)
            self.assertFalse(transport.read_reconciliation_required)

    def test_crash_preserves_process_lease_until_investigated(self):
        with TemporaryDirectory() as root:
            first = ProcessLease("acct-1", directory=root)
            other = ProcessLease("acct-1", directory=root)
            first.acquire()
            with self.assertRaises(StateError):
                other.acquire()
            # No automatic eviction of a stale/unknown owner on restart.
            first.release()
            other.acquire()
            other.release()


if __name__ == "__main__":
    unittest.main()
