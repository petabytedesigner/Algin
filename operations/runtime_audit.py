"""Offline CI/runtime release preflight, deliberately not broker attestation.

Check only verifiable properties: interpreter bounds, installed direct runtime
dependencies, static GitHub workflow safety, and independent broker write
locks. Never read API keys, create orders, call endpoints or claim Termux uptime.
"""
from __future__ import annotations

from importlib import metadata
from pathlib import Path
import json
import platform
import re
import sys

import config
from broker.practice_transport import PRACTICE_API, _PRACTICE_WRITES_RELEASED

REPO_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_PYTHONS = ((3, 11), (3, 12), (3, 13), (3, 14))
RUNTIME_REQUIREMENTS = frozenset(("requests>=2.31,<3", "tzdata>=2025.2,<2028"))
CHECKOUT_ACTION = "11d5960a326750d5838078e36cf38b85af677262"
SETUP_PYTHON_ACTION = "a26af69be951a213d495a4c3e4e4022e16d87065"
_ALLOWED_ACTIONS = frozenset((
    "actions/checkout@" + CHECKOUT_ACTION,
    "actions/setup-python@" + SETUP_PYTHON_ACTION,
))
_APPROVED_PRACTICE_SECRET_STEP = """      - name: Verify authenticated Practice account by GET only
        env:
          OANDA_API_TOKEN: ${{ secrets.OANDA }}
          OANDA_ACCOUNT_ID: ${{ secrets.OANDA_ACCOUNT_ID }}
        run: python -m operations.practice_ci_probe --run-actions"""


def _approved_workflow_secrets(path, content):
    """Permit precisely two secrets in the manual, main-only Practice job."""
    if path.name != "category12.yml":
        return "secrets." not in content
    if content.count(_APPROVED_PRACTICE_SECRET_STEP) != 1:
        return False
    if "secrets." in content.replace(_APPROVED_PRACTICE_SECRET_STEP, "", 1):
        return False
    manual = re.search(
        r"(?ms)^  authenticated-practice-read:\n.*?(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        content,
    )
    return bool(
        manual and _APPROVED_PRACTICE_SECRET_STEP in manual.group()
        and "if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'"
        in manual.group()
    )


def _version(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", value)
    return tuple(int(x or 0) for x in match.groups()) if match else None


def audit_dependencies(*, versions=None, requirements=None):
    """Fail closed on missing/drifted dependencies, not just import success."""
    names = ("requests", "tzdata")
    try:
        installed = (
            {name: metadata.version(name) for name in names}
            if versions is None else dict(versions)
        )
    except metadata.PackageNotFoundError:
        return {"status": "BLOCKED", "reason": "RUNTIME_DEPENDENCY_MISSING"}
    normalized = {name: _version(installed.get(name)) for name in names}
    if (normalized["requests"] is None or normalized["tzdata"] is None
        or not (2, 31, 0) <= normalized["requests"] < (3, 0, 0)
        or not (2025, 2, 0) <= normalized["tzdata"] < (2028, 0, 0)):
        return {"status": "BLOCKED", "reason": "RUNTIME_DEPENDENCY_RANGE"}
    actual = (
        REPO_ROOT.joinpath("requirements.txt").read_text(encoding="utf-8")
        if requirements is None else requirements
    )
    clean = [
        line.strip() for line in actual.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(clean) != len(RUNTIME_REQUIREMENTS) or set(clean) != RUNTIME_REQUIREMENTS:
        return {"status": "BLOCKED", "reason": "RUNTIME_REQUIREMENTS_CHANGED"}
    return {
        "status": "PASS",
        "requests_version": installed["requests"],
        "tzdata_version": installed["tzdata"],
        "transitive_dependencies": "REQUIRE_PIP_CHECK",
        "reproducible_install": "NOT_LOCKED_WITH_HASHES",
    }


def audit_workflows(root=REPO_ROOT):
    """Limited textual policy lint; does not claim a full YAML/security audit."""
    directory = Path(root) / ".github" / "workflows"
    paths = sorted(directory.glob("*.yml"))
    if len(paths) < 11:
        return {"status": "BLOCKED", "reason": "CI_WORKFLOW_INVENTORY_INCOMPLETE"}
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return {"status": "BLOCKED", "reason": "CI_WORKFLOW_UNREADABLE"}
        if (not re.search(r"(?m)^permissions:\s*\n\s+contents:\s*read\s*$", text)
            or re.search(r"(?m)^\s*pull_request_target\s*:", text)
            or "write-all" in text or not _approved_workflow_secrets(path, text)):
            return {"status": "BLOCKED", "reason": "CI_WORKFLOW_PERMISSION_POLICY"}
        actions = re.findall(r"(?m)^\s*-\s*uses:\s*(\S+)", text)
        if (not actions or any(action not in _ALLOWED_ACTIONS for action in actions)
            or "actions/checkout@" + CHECKOUT_ACTION not in actions
            or "actions/setup-python@" + SETUP_PYTHON_ACTION not in actions):
            return {"status": "BLOCKED", "reason": "CI_WORKFLOW_ACTION_NOT_PINNED"}
    practice = (directory / "category12.yml").read_text(encoding="utf-8")
    if ("if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'"
        not in practice or
        "python -m operations.practice_ci_probe --run-actions" not in practice):
        return {"status": "BLOCKED", "reason": "CI_AUTH_PROBE_NOT_MANUAL_MAIN_ONLY"}
    return {
        "status": "PASS",
        "workflow_count": len(paths),
        "policy": "STATIC_TEXT_CHECK_ONLY",
        "external_actions": "IMMUTABLE_COMMIT_REFERENCES",
        "authenticated_probe": "EXPLICIT_MANUAL_MAIN_ONLY",
    }


def run_audit(*, root=REPO_ROOT, python_version=None, versions=None,
              requirements=None):
    version = tuple(sys.version_info[:2]) if python_version is None else tuple(python_version)
    python_ok = version in SUPPORTED_PYTHONS
    writes_locked = (
        config.OANDA_URL == PRACTICE_API
        and config.ORDER_WRITES_ENABLED is False
        and _PRACTICE_WRITES_RELEASED is False
    )
    try:
        dependencies = audit_dependencies(versions=versions, requirements=requirements)
        workflows = audit_workflows(root=root)
    except (OSError, UnicodeError, ValueError, TypeError):
        dependencies = {"status": "BLOCKED", "reason": "OFFLINE_AUDIT_IO_ERROR"}
        workflows = {"status": "BLOCKED", "reason": "OFFLINE_AUDIT_IO_ERROR"}
    passed = (python_ok and writes_locked
              and dependencies["status"] == "PASS"
              and workflows["status"] == "PASS")
    return {
        "status": "OFFLINE_CONTRACT_PASS" if passed else "OFFLINE_CONTRACT_BLOCKED",
        "python": ".".join(map(str, version)),
        "python_policy": "PASS" if python_ok else "UNTESTED_INTERPRETER",
        "platform_system": platform.system(),
        "dependencies": dependencies,
        "workflows": workflows,
        "broker_write_gates": "LOCKED" if writes_locked else "UNSAFE",
        "authenticated_oanda_get": "NOT_TESTED_BY_THIS_AUDIT",
        "fmp_live_source": "NOT_TESTED_BY_THIS_AUDIT",
        "termux_on_device": "NOT_TESTED_BY_GITHUB_HOSTED_RUNNER",
        "uptime_24h": "NOT_MEASURED_ON_DEVICE",
        "trading_performance": "NOT_VERIFIED",
    }


def main():
    report = run_audit()
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "OFFLINE_CONTRACT_PASS" else 3


if __name__ == "__main__":
    sys.exit(main())
