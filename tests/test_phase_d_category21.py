"""Phase D Category 21 operational readiness: NO-GO cannot be laundered.

All tests use offline fixture evidence. No account mutation or network I/O.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import io
import json
import os
import unittest

import config
from operations.forward_journal import ForwardJournal
from operations.release_readiness import (
    DECISION, SCHEMA_VERSION, build_readiness, main,
)

NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
FAKE_ACCOUNT = "101-001-11112222-003"
FAKE_TOKEN = "secret-this-is-only-synthetic-test-value"


def offline_contract():
    return {
        "status": "OFFLINE_CONTRACT_PASS", "broker_write_gates": "LOCKED",
        "authenticated_oanda_get": "NOT_TESTED_BY_THIS_AUDIT",
        "termux_on_device": "NOT_TESTED_BY_GITHUB_HOSTED_RUNNER",
    }


class OperationalNoGoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.local = self.root / ".local"
        self.local.mkdir()
        self.fake_secret = self.root / ".env.example"
        self.fake_secret.write_text(
            "OANDA_API_TOKEN=" + FAKE_TOKEN + "\n"
            + "OANDA_ACCOUNT_ID=" + FAKE_ACCOUNT + "\n"
        )

    def assess(self, *, policy=None, forward=None):
        return build_readiness(
            root=self.root, policy_report=offline_contract()
            if policy is None else policy,
            forward_report=forward,
        )

    def test_green_offline_contract_never_becomes_trade_authorization(self):
        r = self.assess()
        self.assertEqual(r["schema_version"], SCHEMA_VERSION)
        self.assertEqual(r["decision"], DECISION)
        self.assertFalse(r["execution_authorized"])
        self.assertEqual(r["broker_mutations"], 0)
        self.assertEqual(r["observe_only_code_status"], "READ_ONLY_CODE_POLICY_PASS")
        self.assertEqual(r["gate_checks"]["offline_code_contract"], "PASS")
        self.assertEqual(r["gate_checks"]["independent_broker_write_locks"], "LOCKED")
        self.assertEqual(r["gate_checks"]["authenticated_oanda_get"],
                         "NOT_ATTESTED_BY_LOCAL_AUDIT")
        self.assertEqual(r["gate_checks"]["manual_operational_authorization"],
                         "NOT_GRANTED_BY_CODE")
        self.assertIn("AUTOMATED_BROKER_WRITES_NOT_RELEASED", r["blockers"])
        self.assertIn("OPERATOR_APPROVAL_AND_INDEPENDENT_KILL_SWITCH", r["blockers"])

    def test_unverified_live_dependency_gates_remain_explicit(self):
        r = self.assess()
        for requirement in (
            "AUTHENTICATED_PRACTICE_READ",
            "SAME_GENERATION_ACCOUNT_RECONCILIATION",
            "PENDING_ORDERS_AND_SL_TP_PROTECTION",
            "FRESH_ACCOUNT_RISK_AND_NAV",
            "LIVE_ECONOMIC_CALENDAR",
            "EXTERNAL_TERMUX_ENDURANCE_24H",
            "UNKNOWN_OUTCOME_AND_CRASH_RECOVERY_DRILL",
            "CONTROLLED_PRACTICE_ORDER_LIFECYCLE",
            "CI_MAIN_COMMIT_PROVENANCE",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, r["blockers"])
        self.assertEqual(r["recovery_policy"], "STOP_RECONCILE_NO_BLIND_RETRY")
        self.assertEqual(r["financial_performance"], "NOT_VERIFIED")
        self.assertEqual(r["on_device_uptime"], "NOT_MEASURED")

    def test_claimed_network_success_in_injected_report_never_releases_trades(self):
        forged = {
            **offline_contract(),
            "authenticated_oanda_get": "AUTHENTICATED_PRACTICE_VERIFIED",
            "broker_proven": True, "fmp_live_source": "VERIFIED",
        }
        r = self.assess(policy=forged)
        self.assertFalse(r["execution_authorized"])
        self.assertEqual(r["decision"], DECISION)
        self.assertEqual(r["gate_checks"]["authenticated_oanda_get"],
                         "NOT_ATTESTED_BY_LOCAL_AUDIT")

    def test_unhealthy_policy_is_not_laundered_by_heartbeat(self):
        invented_forward = {
            "status": "OBSERVATION_ONLY_NOT_BROKER_PROOF",
            "completed_cycle_samples": 4000,
            "broker_verified_cycles": 0,
            "uptime_24h": "NOT_MEASURED_ON_DEVICE",
        }
        for broken in (
            {"status": "OFFLINE_CONTRACT_BLOCKED",
             "broker_write_gates": "LOCKED"},
            {"status": "OFFLINE_CONTRACT_PASS",
             "broker_write_gates": "UNSAFE"},
        ):
            with self.subTest(broken=broken):
                r = self.assess(policy=broken, forward=invented_forward)
                self.assertFalse(r["execution_authorized"])
                self.assertEqual(r["observe_only_code_status"],
                                 "OFFLINE_POLICY_UNVERIFIED_OR_UNSAFE")
                self.assertIn("OFFLINE_POLICY_UNVERIFIED_OR_UNSAFE",
                              r["blockers"])

    def test_unsafe_config_gates_fail_closed_even_if_policy_report_is_forged(self):
        for setting in (
            {"ORDER_WRITES_ENABLED": True},
            {"OANDA_URL": "https://api-fxtrade.oanda.com/v3"},
        ):
            with self.subTest(setting=setting), patch.multiple(config, **setting):
                r = self.assess()
                self.assertEqual(r["gate_checks"]["independent_broker_write_locks"],
                                 "UNSAFE")
                self.assertFalse(r["execution_authorized"])
                self.assertEqual(r["observe_only_code_status"],
                                 "OFFLINE_POLICY_UNVERIFIED_OR_UNSAFE")

    def test_forward_completed_events_remain_local_telemetry_only(self):
        journal = ForwardJournal(self.local / "forward-runs")
        try:
            journal.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
            journal.emit("SAMPLE", "CYCLE_COMPLETE",
                         cycle_id="cycle-1", now=NOW + timedelta(seconds=1))
            journal.emit("STOP", "SAMPLE_WINDOW_COMPLETE",
                         now=NOW + timedelta(seconds=2))
        finally:
            journal.close()
        report = self.assess()
        self.assertEqual(report["local_forward"]["completed_cycle_samples"], 1)
        self.assertEqual(report["local_forward"]["broker_verified_cycles"], 0)
        self.assertEqual(report["local_forward"]["uptime_24h"],
                         "NOT_MEASURED_ON_DEVICE")
        self.assertEqual(report["local_forward"]["status"],
                         "OBSERVATION_ONLY_NOT_BROKER_PROOF")
        self.assertFalse(report["execution_authorized"])

    def test_corrupt_forward_history_is_incident_not_go(self):
        path = self.local / "forward-runs"
        path.mkdir()
        (path / ("c" * 32 + ".jsonl")).write_text(
            "{\"broken\":\"unverified\"}\n"
        )
        report = self.assess()
        self.assertEqual(report["local_forward"]["status"],
                         "FORWARD_EVIDENCE_UNTRUSTED")
        self.assertIn("FORWARD_JOURNAL_INCIDENT_UNRESOLVED",
                      report["blockers"])
        self.assertFalse(report["execution_authorized"])

    def test_blocked_forward_history_requires_operator_review(self):
        journal = ForwardJournal(self.local / "forward-runs")
        try:
            journal.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
            journal.emit("BLOCKED", "BOT_BROKER_UNAVAILABLE",
                         now=NOW + timedelta(seconds=1))
        finally:
            journal.close()
        result = self.assess()
        self.assertEqual(result["local_forward"]["status"],
                         "OBSERVATION_INCIDENT_REQUIRES_REVIEW")
        self.assertIn("FORWARD_JOURNAL_INCIDENT_UNRESOLVED",
                      result["blockers"])

    def test_demo_and_supervisor_artifacts_are_not_claimed_as_broker_proof(self):
        demo = self.local / "category12_one_shot.jsonl"
        demo.write_text("SYNTHETIC_TOKEN=" + FAKE_TOKEN)
        lock = self.local / "forward-supervisor.lock"
        lock.write_text("pid=1:synthetic")
        original = (demo.read_bytes(), lock.read_bytes())
        report = self.assess()
        self.assertEqual(
            report["local_category12_demo_file"],
            "FILE_PRESENT_NOT_LIVE_ATTESTATION"
        )
        self.assertEqual(
            report["forward_supervisor_lock"],
            "LOCK_PRESENT_OWNERSHIP_NOT_ASSESSED"
        )
        self.assertEqual((demo.read_bytes(), lock.read_bytes()), original)
        self.assertEqual(report["decision"], DECISION)
        self.assertNotIn(FAKE_TOKEN, json.dumps(report))

    def test_separate_category12_arm_is_detected_but_never_executed(self):
        with patch.dict(os.environ, {
            "ALGIN_PRACTICE_ONE_SHOT_CONFIRM": "synthetic-armed-value"
        }):
            result = self.assess()
        self.assertEqual(result["category12_manual_demo_arm"],
                         "EXTERNAL_DEMO_ARM_VARIABLE_PRESENT")
        self.assertIn("SEPARATE_MANUAL_DEMO_ARM_REQUIRES_OPERATOR_REVIEW",
                      result["blockers"])
        self.assertFalse(result["execution_authorized"])

    def test_cli_report_is_zero_only_for_successful_no_go_assessment(self):
        out = io.StringIO()
        with redirect_stdout(out), patch(
            "operations.release_readiness.run_audit",
            return_value=offline_contract()
        ), patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Broker HTTP not permitted")
        ):
            rc = main(["--report"], root=self.root)
        self.assertEqual(rc, 0)
        r = json.loads(out.getvalue())
        self.assertEqual(r["decision"], DECISION)
        self.assertFalse(r["execution_authorized"])
        self.assertNotIn(FAKE_TOKEN, out.getvalue())
        self.assertNotIn(FAKE_ACCOUNT, out.getvalue())

    def test_cli_require_execution_ready_is_always_nonzero(self):
        out = io.StringIO()
        with redirect_stdout(out), patch(
            "operations.release_readiness.run_audit",
            return_value=offline_contract()
        ), patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Network forbidden")
        ):
            rc = main(["--require-execution-ready"], root=self.root)
        self.assertEqual(rc, 3)
        self.assertEqual(json.loads(out.getvalue())["decision"], DECISION)
        self.assertFalse(json.loads(out.getvalue())["execution_authorized"])

    def test_cli_policy_failure_is_nonzero_and_sanitized(self):
        out = io.StringIO()
        with redirect_stdout(out), patch(
            "operations.release_readiness.run_audit",
            return_value={"status": "OFFLINE_CONTRACT_BLOCKED",
                          "broker_write_gates": "UNSAFE"}
        ):
            rc = main(["--report"], root=self.root)
        self.assertEqual(rc, 3)
        result = json.loads(out.getvalue())
        self.assertFalse(result["execution_authorized"])
        self.assertNotIn(FAKE_TOKEN, out.getvalue())

    def test_cli_unexpected_error_does_not_expose_untrusted_exception(self):
        out = io.StringIO()
        with redirect_stdout(out), patch(
            "operations.release_readiness.run_audit",
            side_effect=RuntimeError(FAKE_TOKEN)
        ):
            rc = main(["--report"], root=self.root)
        self.assertEqual(rc, 3)
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "ASSESSMENT_FAILED_FAIL_CLOSED")
        self.assertNotIn(FAKE_TOKEN, out.getvalue())

    def test_readiness_never_edits_env_files_or_creates_local_evidence(self):
        before = self.fake_secret.read_bytes()
        files_before = sorted(str(p.relative_to(self.root))
                              for p in self.root.rglob("*"))
        report = self.assess()
        files_after = sorted(str(p.relative_to(self.root))
                             for p in self.root.rglob("*"))
        self.assertEqual(before, self.fake_secret.read_bytes())
        self.assertEqual(files_before, files_after)
        self.assertEqual(report["broker_mutations"], 0)


if __name__ == "__main__":
    unittest.main()
