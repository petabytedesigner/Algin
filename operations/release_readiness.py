"""Category 21: read-only and fail-closed operational release assessment.

A green CI build, a valid heartbeat, or an authenticated GET must never grant
execution permission. This tool deliberately has *no* GO_FOR_EXECUTION result,
write operation, broker credential loader, or approval/override parameter.

The separate Category 12 technical one-shot demo is not an automated release.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import config
from broker.practice_transport import PRACTICE_API, _PRACTICE_WRITES_RELEASED
from operations.forward_journal import ForwardEvidenceError, report_runs
from operations.runtime_audit import REPO_ROOT, run_audit


DECISION = "NO_GO_AUTONOMOUS_EXECUTION"
SCHEMA_VERSION = 1
_UNVERIFIED_GATES = (
    "CI_MAIN_COMMIT_PROVENANCE",
    "AUTHENTICATED_PRACTICE_READ",
    "SAME_GENERATION_ACCOUNT_RECONCILIATION",
    "PENDING_ORDERS_AND_SL_TP_PROTECTION",
    "FRESH_ACCOUNT_RISK_AND_NAV",
    "LIVE_ECONOMIC_CALENDAR",
    "EXTERNAL_TERMUX_ENDURANCE_24H",
    "UNKNOWN_OUTCOME_AND_CRASH_RECOVERY_DRILL",
    "CONTROLLED_PRACTICE_ORDER_LIFECYCLE",
    "OPERATOR_APPROVAL_AND_INDEPENDENT_KILL_SWITCH",
)


def _forward_observation(root):
    """Summarize verified local journal structure, not external uptime."""
    try:
        report = report_runs(Path(root) / ".local" / "forward-runs")
    except (ForwardEvidenceError, OSError, UnicodeError, ValueError):
        return {
            "status": "FORWARD_EVIDENCE_UNTRUSTED",
            "completed_cycle_samples": 0, "interrupted_runs": None,
            "blocked_runs": None, "broker_verified_cycles": 0,
            "uptime_24h": "NOT_MEASURED_ON_DEVICE",
        }

    if report.get("status") == "NO_OBSERVATION_EVIDENCE":
        status = "NO_OBSERVATION_EVIDENCE"
    elif (report.get("status") == "UNVERIFIED_INTERRUPTED_RUNS"
          or report.get("blocked_runs", 0) > 0):
        status = "OBSERVATION_INCIDENT_REQUIRES_REVIEW"
    else:
        status = "OBSERVATION_ONLY_NOT_BROKER_PROOF"

    return {
        "status": status,
        "completed_cycle_samples": report.get("completed_cycle_samples", 0),
        "interrupted_runs": report.get("interrupted_runs", 0),
        "blocked_runs": report.get("blocked_runs", 0),
        "broker_verified_cycles": 0,
        "uptime_24h": "NOT_MEASURED_ON_DEVICE",
    }


def build_readiness(*, root=REPO_ROOT, policy_report=None,
                    forward_report=None):
    """Immutable execution NO-GO regardless of synthetic/legacy evidence.

    Policy and journal injection exists for deterministic offline unit tests;
    the CLI always reads the actual local policy and existing journal files.
    Neither input is capable of granting broker write permission.
    """
    root = Path(root)
    if policy_report is None:
        policy_report = run_audit(root=root)
    forward = (_forward_observation(root) if forward_report is None
               else dict(forward_report))
    policy_ok = (
        isinstance(policy_report, dict)
        and policy_report.get("status") == "OFFLINE_CONTRACT_PASS"
        and policy_report.get("broker_write_gates") == "LOCKED"
    )
    # Defense-in-depth: do not trust a claimed policy report if the actual
    # broker policy has been changed in this running Python interpreter.
    hard_locked = (
        config.ORDER_WRITES_ENABLED is False
        and _PRACTICE_WRITES_RELEASED is False
        and config.OANDA_URL == PRACTICE_API
    )
    local_status = (
        "READ_ONLY_CODE_POLICY_PASS" if policy_ok and hard_locked
        else "OFFLINE_POLICY_UNVERIFIED_OR_UNSAFE"
    )
    blockers = list(_UNVERIFIED_GATES)
    if not policy_ok or not hard_locked:
        blockers.insert(0, "OFFLINE_POLICY_UNVERIFIED_OR_UNSAFE")
    else:
        blockers.insert(0, "AUTOMATED_BROKER_WRITES_NOT_RELEASED")

    if forward.get("status") in (
        "FORWARD_EVIDENCE_UNTRUSTED",
        "OBSERVATION_INCIDENT_REQUIRES_REVIEW",
    ):
        blockers.append("FORWARD_JOURNAL_INCIDENT_UNRESOLVED")

    # This file can contain historical technical-demo output, but its
    # presence is not proof of a clean *current* broker account.
    technical_demo = root / ".local" / "category12_one_shot.jsonl"
    demo_evidence = (
        "FILE_PRESENT_NOT_LIVE_ATTESTATION"
        if technical_demo.is_symlink() or technical_demo.exists()
        else "NO_LOCAL_FILE"
    )
    supervisor_lock = root / ".local" / "forward-supervisor.lock"
    lock_status = (
        "LOCK_PRESENT_OWNERSHIP_NOT_ASSESSED"
        if supervisor_lock.is_symlink() or supervisor_lock.exists()
        else "LOCK_NOT_OBSERVED_NOT_PROCESS_PROOF"
    )
    one_shot_arm = (
        "EXTERNAL_DEMO_ARM_VARIABLE_PRESENT"
        if os.environ.get("ALGIN_PRACTICE_ONE_SHOT_CONFIRM")
        else "NOT_OBSERVED"
    )
    if one_shot_arm != "NOT_OBSERVED":
        blockers.append("SEPARATE_MANUAL_DEMO_ARM_REQUIRES_OPERATOR_REVIEW")

    return {
        "schema_version": SCHEMA_VERSION,
        "decision": DECISION,
        "assessment_mode": "READ_ONLY_LOCAL_EVIDENCE",
        "observe_only_code_status": local_status,
        "execution_authorized": False,
        "broker_mutations": 0,
        "gate_checks": {
            "offline_code_contract": (
                "PASS" if policy_ok and hard_locked else "UNVERIFIED_OR_UNSAFE"
            ),
            "independent_broker_write_locks": (
                "LOCKED" if hard_locked else "UNSAFE"
            ),
            "github_ci_for_current_main_commit": "NOT_ATTESTED_BY_LOCAL_AUDIT",
            "authenticated_oanda_get": "NOT_ATTESTED_BY_LOCAL_AUDIT",
            "account_and_order_reconciliation": "NOT_ATTESTED_BY_LOCAL_AUDIT",
            "economic_calendar_live": "NOT_ATTESTED_BY_LOCAL_AUDIT",
            "termux_on_device_24h": "NOT_ATTESTED_BY_LOCAL_AUDIT",
            "risk_history_nav_and_open_exposure": "NOT_ATTESTED_BY_LOCAL_AUDIT",
            "unknown_execution_outcome_recovery": "NOT_ATTESTED_BY_LOCAL_AUDIT",
            "manual_operational_authorization": "NOT_GRANTED_BY_CODE",
        },
        "local_forward": forward,
        "local_category12_demo_file": demo_evidence,
        "forward_supervisor_lock": lock_status,
        "category12_manual_demo_arm": one_shot_arm,
        "blockers": blockers,
        "recovery_policy": "STOP_RECONCILE_NO_BLIND_RETRY",
        "required_next_stage": "SEPARATE_OPERATOR_REVIEW_AND_CODE_RELEASE",
        "fmp_subscription": "NOT_PROVEN_BY_LOCAL_AUDIT",
        "financial_performance": "NOT_VERIFIED",
        "on_device_uptime": "NOT_MEASURED",
    }


def main(argv=None, *, root=REPO_ROOT):
    parser = argparse.ArgumentParser(
        description="Read-only Category 21 readiness assessment"
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--report", action="store_true",
        help="Print NO-GO/observation inventory; return 0 if report generated"
    )
    modes.add_argument(
        "--require-execution-ready", action="store_true",
        help="Always nonzero until a separately reviewed release exists"
    )
    args = parser.parse_args(argv)
    try:
        result = build_readiness(root=root)
    except Exception:
        # Never include raw exception text or local files in CLI output.
        print(json.dumps({
            "schema_version": SCHEMA_VERSION, "decision": DECISION,
            "execution_authorized": False, "broker_mutations": 0,
            "status": "ASSESSMENT_FAILED_FAIL_CLOSED",
        }, sort_keys=True))
        return 3
    print(json.dumps(result, sort_keys=True))
    if args.require_execution_ready:
        return 3
    # The successful *creation of a NO-GO report* is not a GO signal.
    return 0 if result["observe_only_code_status"] == "READ_ONLY_CODE_POLICY_PASS" else 3


if __name__ == "__main__":
    sys.exit(main())
