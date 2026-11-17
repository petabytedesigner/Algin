"""Phase D Category 16: CandleGate lock ownership and concurrency hardening.

All tests use temporary local files. No credentials, broker requests or writes.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from data.candle_gate import CandleGate, CandleGateError


CANDLE = "2026-11-15T09:45:00Z"
NEXT = "2026-11-15T10:00:00Z"


class CandleGateOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gate = CandleGate("acct-1", directory=self.tmp.name)
        self.gate.lock.parent.mkdir(parents=True, exist_ok=True)

    def test_normal_claim_restart_and_lock_cleanup_preserve_contract(self):
        self.assertTrue(self.gate.claim("EUR_USD", CANDLE))
        self.assertFalse(self.gate.lock.exists())
        restarted = CandleGate("acct-1", directory=self.tmp.name)
        self.assertFalse(restarted.claim("EUR_USD", CANDLE))
        self.assertTrue(restarted.claim("EUR_USD", NEXT))
        self.assertTrue(restarted.claim("GBP_USD", CANDLE))
        self.assertFalse(self.gate.lock.exists())
        self.assertFalse(restarted.lock.exists())

    def test_competing_writer_blocked_while_owner_is_inside(self):
        entered = threading.Event()
        release = threading.Event()
        real_read = self.gate._read

        def blocked_read():
            entered.set()
            if not release.wait(5):
                raise AssertionError("Owner release timed out")
            return real_read()

        contender = CandleGate("acct-1", directory=self.tmp.name)
        with patch.object(self.gate, "_read", side_effect=blocked_read):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.gate.claim, "EUR_USD", CANDLE)
                try:
                    self.assertTrue(entered.wait(5))
                    with self.assertRaisesRegex(CandleGateError, "Concurrent or orphaned"):
                        contender.claim("EUR_USD", CANDLE)
                finally:
                    release.set()
                self.assertTrue(first.result(timeout=5))
        self.assertFalse(contender.claim("EUR_USD", CANDLE))
        self.assertFalse(self.gate.lock.exists())

    def test_identical_owner_text_in_replaced_inode_fails_before_write(self):
        original_read = self.gate._read
        backup = self.gate.lock.with_suffix(".old")

        def substitute_same_token():
            data = original_read()
            proof = self.gate.lock.read_text(encoding="ascii")
            self.gate.lock.rename(backup)
            self.gate.lock.write_text(proof, encoding="ascii")
            return data

        with patch.object(self.gate, "_read", side_effect=substitute_same_token):
            with self.assertRaisesRegex(CandleGateError, "ownership changed"):
                self.gate.claim("EUR_USD", CANDLE)
        self.assertTrue(self.gate.lock.exists())
        self.assertTrue(backup.exists())
        self.assertEqual(self.gate.lock.read_text(encoding="ascii"),
                         backup.read_text(encoding="ascii"))
        self.assertFalse(self.gate.path.exists())
        with self.assertRaises(CandleGateError):
            CandleGate("acct-1", directory=self.tmp.name).claim("EUR_USD", CANDLE)

    def test_inplace_owner_tamper_fails_before_checkpoint(self):
        original_read = self.gate._read

        def tamper():
            state = original_read()
            self.gate.lock.write_text("pid=999 owner=foreign\n", encoding="ascii")
            return state

        with patch.object(self.gate, "_read", side_effect=tamper):
            with self.assertRaisesRegex(CandleGateError, "ownership changed"):
                self.gate.claim("EUR_USD", CANDLE)
        self.assertTrue(self.gate.lock.exists())
        self.assertEqual(self.gate.lock.read_text(), "pid=999 owner=foreign\n")
        self.assertFalse(self.gate.path.exists())

    def test_replacement_after_durable_checkpoint_is_not_unlinked(self):
        real_write = self.gate._write
        backup = self.gate.lock.with_suffix(".original")

        def replace_after_write(data):
            real_write(data)
            self.gate.lock.rename(backup)
            self.gate.lock.write_text("pid=999 owner=foreign\n", encoding="ascii")

        with patch.object(self.gate, "_write", side_effect=replace_after_write):
            with self.assertRaisesRegex(CandleGateError, "ownership changed"):
                self.gate.claim("EUR_USD", CANDLE)
        self.assertTrue(self.gate.path.exists())
        self.assertTrue(self.gate.lock.exists())
        self.assertEqual(self.gate.lock.read_text(), "pid=999 owner=foreign\n")
        # Once an operator resolves this synthetic orphan, the durable claim
        # must remain consumed: a misleading exception must not resend it.
        self.gate.lock.unlink()
        self.assertFalse(CandleGate("acct-1", directory=self.tmp.name)
                         .claim("EUR_USD", CANDLE))

    def test_failed_initial_lock_fsync_leaves_orphan_fail_closed(self):
        with patch("data.candle_gate.os.fsync",
                   side_effect=OSError("simulated lock fsync error")):
            with self.assertRaises(OSError):
                self.gate.claim("EUR_USD", CANDLE)
        self.assertTrue(self.gate.lock.exists())
        self.assertFalse(self.gate.path.exists())
        with self.assertRaisesRegex(CandleGateError, "Concurrent or orphaned"):
            self.gate.claim("EUR_USD", CANDLE)

    def test_symlink_lock_is_never_followed_or_unlinked(self):
        target = Path(self.tmp.name) / "external-data"
        target.write_text("do not touch", encoding="utf-8")
        self.gate.lock.symlink_to(target)
        with self.assertRaises(CandleGateError):
            self.gate.claim("EUR_USD", CANDLE)
        self.assertTrue(self.gate.lock.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "do not touch")
        self.assertFalse(self.gate.path.exists())

    def test_existing_unowned_lock_remains_untouched(self):
        self.gate.lock.write_text("orphan", encoding="ascii")
        with self.assertRaisesRegex(CandleGateError, "Concurrent or orphaned"):
            self.gate.claim("EUR_USD", CANDLE)
        self.assertEqual(self.gate.lock.read_text(encoding="ascii"), "orphan")
        self.assertFalse(self.gate.path.exists())


if __name__ == "__main__":
    unittest.main()
