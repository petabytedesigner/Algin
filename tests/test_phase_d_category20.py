"""Phase D Category 20: offline CI/Termux audit and explicit Practice probe.

No network calls, real token access, broker mutations or device uptime claims.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import io
import json
import shutil
import unittest

import config
from broker.practice_transport import PRACTICE_API
from operations import practice_ci_probe, runtime_audit
from operations.forward_supervisor import EXPECTED_ACCOUNT

FAKE_TOKEN = "synthetic-category20-test-token-0123456789"
BOUNDS = "requests>=2.31,<3\ntzdata>=2025.2,<2028\n"
DEPS = {"requests": "2.32.5", "tzdata": "2025.3"}


class DependencyAndWorkflowAuditTests(unittest.TestCase):
    def test_exact_supported_runtime_dependency_bounds(self):
        report = runtime_audit.audit_dependencies(
            versions=DEPS, requirements=BOUNDS
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["reproducible_install"],
                         "NOT_LOCKED_WITH_HASHES")

    def test_missing_unexpected_and_unapproved_requirements_block(self):
        for versions, requirements in (
            ({}, BOUNDS),
            ({"requests": "2.30.9", "tzdata": "2025.3"}, BOUNDS),
            ({"requests": "3.0.0", "tzdata": "2025.3"}, BOUNDS),
            ({"requests": "2.32.5", "tzdata": "2024.1"}, BOUNDS),
            ({"requests": "2.32.5", "tzdata": "2028.0"}, BOUNDS),
            ({"requests": "2.32.5", "tzdata": "prerelease"}, BOUNDS),
            (DEPS, BOUNDS + "unreviewed-package>=1\n"),
            (DEPS, "requests>=2.31\ntzdata>=2025.2,<2028\n"),
        ):
            with self.subTest(versions=versions, requirements=requirements):
                self.assertEqual(
                    runtime_audit.audit_dependencies(
                        versions=versions, requirements=requirements
                    )["status"], "BLOCKED"
                )

    def test_registered_workflow_set_has_pinned_actions_and_read_permissions(self):
        result = runtime_audit.audit_workflows()
        self.assertEqual(result["status"], "PASS")
        self.assertGreaterEqual(result["workflow_count"], 12)
        self.assertEqual(result["policy"], "STATIC_TEXT_CHECK_ONLY")
        self.assertEqual(result["authenticated_probe"],
                         "EXPLICIT_MANUAL_MAIN_ONLY")

    def copy_workflows(self, target):
        path = Path(target)
        shutil.copytree(
            runtime_audit.REPO_ROOT / ".github" / "workflows",
            path / ".github" / "workflows"
        )
        return path / ".github" / "workflows"

    def test_mutable_action_or_unsafe_pr_trigger_fails_audit(self):
        for replacement in (
            "actions/checkout@v4",
            "actions/checkout@0000000000000000000000000000000000000000",
        ):
            with self.subTest(action=replacement), TemporaryDirectory() as tmp:
                folder = self.copy_workflows(tmp)
                src = folder / "category1.yml"
                src.write_text(src.read_text().replace(
                    "actions/checkout@" + runtime_audit.CHECKOUT_ACTION,
                    replacement
                ))
                self.assertEqual(
                    runtime_audit.audit_workflows(tmp)["status"], "BLOCKED"
                )
        with TemporaryDirectory() as tmp:
            folder = self.copy_workflows(tmp)
            src = folder / "category4.yml"
            src.write_text(src.read_text() + "\npull_request_target:\n")
            self.assertEqual(
                runtime_audit.audit_workflows(tmp)["status"], "BLOCKED"
            )

    def test_mutated_workflow_permissions_and_manual_probe_gate_rejected(self):
        with TemporaryDirectory() as tmp:
            folder = self.copy_workflows(tmp)
            src = folder / "category3.yml"
            src.write_text(src.read_text().replace(
                "  contents: read", "  contents: write"
            ))
            self.assertEqual(
                runtime_audit.audit_workflows(tmp)["status"], "BLOCKED"
            )
        with TemporaryDirectory() as tmp:
            folder = self.copy_workflows(tmp)
            src = folder / "category12.yml"
            src.write_text(src.read_text().replace(
                "if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'",
                "if: github.event_name == 'push'"
            ))
            self.assertEqual(
                runtime_audit.audit_workflows(tmp)["status"], "BLOCKED"
            )

    def test_unsupported_python_version_is_not_incorrectly_certified(self):
        report = runtime_audit.run_audit(
            python_version=(3, 15), versions=DEPS, requirements=BOUNDS
        )
        self.assertEqual(report["status"], "OFFLINE_CONTRACT_BLOCKED")
        self.assertEqual(report["python_policy"], "UNTESTED_INTERPRETER")

    def test_write_gates_are_independent_fail_closed(self):
        for edit in (
            {"ORDER_WRITES_ENABLED": True},
            {"OANDA_URL": "https://api-fxtrade.oanda.com/v3"},
        ):
            with self.subTest(edit=edit), patch.multiple(config, **edit):
                report = runtime_audit.run_audit(
                    python_version=(3, 11),
                    versions=DEPS, requirements=BOUNDS
                )
                self.assertEqual(report["status"], "OFFLINE_CONTRACT_BLOCKED")
                self.assertEqual(report["broker_write_gates"], "UNSAFE")

    def test_report_never_promotes_ci_to_broker_or_termux_proof(self):
        report = runtime_audit.run_audit(
            python_version=(3, 11), versions=DEPS, requirements=BOUNDS
        )
        self.assertEqual(report["status"], "OFFLINE_CONTRACT_PASS")
        self.assertEqual(report["broker_write_gates"], "LOCKED")
        self.assertEqual(report["authenticated_oanda_get"],
                         "NOT_TESTED_BY_THIS_AUDIT")
        self.assertEqual(report["fmp_live_source"],
                         "NOT_TESTED_BY_THIS_AUDIT")
        self.assertEqual(report["termux_on_device"],
                         "NOT_TESTED_BY_GITHUB_HOSTED_RUNNER")
        self.assertEqual(report["uptime_24h"], "NOT_MEASURED_ON_DEVICE")
        self.assertNotIn(FAKE_TOKEN, json.dumps(report))


class ExistingDemoPracticeProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / ".env.example"
        self.path.write_text(
            "OANDA_API_TOKEN=" + FAKE_TOKEN + "\n"
            + "OANDA_ACCOUNT_ID=" + EXPECTED_ACCOUNT + "\n"
        )

    def execute(self, arguments, *, source="OANDA_PRACTICE_NETWORK",
                probe_error=None):
        report = SimpleNamespace(
            source=source, environment="PRACTICE",
            account_read_verified=True, summary_read_verified=True,
            instrument_catalog_verified=True, broker_writes_enabled=False,
            public=lambda: {"source": source, "account_suffix": "003"},
        )
        output = io.StringIO()
        with (patch.object(config, "API_KEY", ""),
              patch.object(config, "ACCOUNT_ID", ""),
              patch("broker.practice_readonly_probe.PracticeReadOnlyProbe") as mock,
              redirect_stdout(output)):
            mock.return_value.run.return_value = report
            if probe_error is not None:
                mock.return_value.run.side_effect = probe_error
            status = practice_ci_probe.main(arguments, root=self.root)
            in_process_key = config.API_KEY
            constructor_calls = mock.call_count
        return status, json.loads(output.getvalue()), in_process_key, constructor_calls

    def test_offline_check_never_opens_credentials_or_spawns_probe(self):
        self.path.unlink()
        status, result, key, calls = self.execute(["--check"])
        self.assertEqual(status, 0)
        self.assertEqual(result["status"], "PRACTICE_OFFLINE_POLICY_PASS")
        self.assertEqual(result["authenticated_get"], "NOT_RUN")
        self.assertEqual(calls, 0)
        self.assertEqual(key, "")

    def test_explicit_run_uses_existing_file_only_and_prints_no_secret(self):
        status, result, key, calls = self.execute(["--run"])
        self.assertEqual(status, 0)
        self.assertEqual(result["status"], "AUTHENTICATED_PRACTICE_VERIFIED")
        self.assertEqual(key, FAKE_TOKEN)
        self.assertEqual(calls, 1)
        self.assertNotIn(FAKE_TOKEN, json.dumps(result))
        self.assertNotIn(EXPECTED_ACCOUNT, json.dumps(result))
        self.assertEqual(self.path.read_text(), (
            "OANDA_API_TOKEN=" + FAKE_TOKEN + "\n"
            + "OANDA_ACCOUNT_ID=" + EXPECTED_ACCOUNT + "\n"
        ))

    def test_injected_broker_probe_can_never_prove_network(self):
        status, result, _, calls = self.execute(
            ["--run"], source="OFFLINE_INJECTED"
        )
        self.assertEqual(status, 3)
        self.assertEqual(result["status"], "AUTHENTICATED_PRACTICE_UNVERIFIED")
        self.assertEqual(calls, 1)

    def test_probe_exception_is_sanitized_and_has_no_retry(self):
        status, result, _, calls = self.execute(
            ["--run"], probe_error=RuntimeError(FAKE_TOKEN + " private")
        )
        self.assertEqual(status, 3)
        self.assertEqual(calls, 1)
        self.assertEqual(result["broker_mutations"], 0)
        self.assertNotIn(FAKE_TOKEN, json.dumps(result))

    def test_missing_wrong_and_symlinked_demo_file_fail_before_network(self):
        for change in ("missing", "wrong_account", "symlink", "unexpected_key"):
            with self.subTest(change=change):
                self.path.unlink(missing_ok=True)
                if change == "wrong_account":
                    self.path.write_text(
                        "OANDA_API_TOKEN=" + FAKE_TOKEN + "\n"
                        "OANDA_ACCOUNT_ID=foreign\n"
                    )
                elif change == "symlink":
                    target = self.root / "outside"
                    target.write_text(
                        "OANDA_API_TOKEN=" + FAKE_TOKEN + "\n"
                        "OANDA_ACCOUNT_ID=" + EXPECTED_ACCOUNT + "\n"
                    )
                    self.path.symlink_to(target)
                elif change == "unexpected_key":
                    self.path.write_text(
                        "OANDA_API_TOKEN=" + FAKE_TOKEN + "\n"
                        "OANDA_ACCOUNT_ID=" + EXPECTED_ACCOUNT + "\n"
                        "MALICIOUS_COMMAND=1\n"
                    )
                status, output, _, calls = self.execute(["--run"])
                self.assertEqual(status, 3)
                self.assertEqual(output["status"], "AUTHENTICATED_PRACTICE_UNVERIFIED")
                self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
