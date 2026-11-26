"""Explicit GET-only OANDA Practice evidence from approved credentials.

Never shell-source, print, copy to GitHub outputs, or modify the user's
credentials. Explicit --run-actions uses GitHub environment secrets; local
--run retains the existing demo-file contract. Import and --check are offline.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import config
from operations.forward_supervisor import (
    EXPECTED_ACCOUNT, REPO_ROOT, account_environment, verify_readonly,
)


def main(argv=None, *, root=REPO_ROOT):
    parser = argparse.ArgumentParser(
        description="Explicit OANDA Practice read-only verification"
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true",
                       help="Validate read-only policy without credentials or network")
    modes.add_argument("--run", action="store_true",
                       help="Use existing local demo file for GET-only probe")
    modes.add_argument("--run-actions", action="store_true",
                       help="Use manual GitHub Actions secrets for GET-only probe")
    args = parser.parse_args(argv)

    if args.check:
        try:
            verify_readonly()
        except Exception:
            print(json.dumps({
                "status": "PRACTICE_OFFLINE_POLICY_FAILED", "broker_mutations": 0
            }, sort_keys=True))
            return 3
        print(json.dumps({
            "status": "PRACTICE_OFFLINE_POLICY_PASS",
            "authenticated_get": "NOT_RUN", "broker_mutations": 0,
        }, sort_keys=True))
        return 0

    try:
        # Explicit manual Actions mode requires BOTH repository secrets.
        # Never fall back to a checked-in example file when either is missing.
        # The original --run contract remains unchanged for local operators.
        if args.run_actions:
            if os.environ.get("GITHUB_ACTIONS") != "true":
                raise ValueError("GitHub Actions mode requires GitHub runner")
            action_env = {
                "OANDA_API_TOKEN": os.environ.get("OANDA_API_TOKEN", ""),
                "OANDA_ACCOUNT_ID": os.environ.get("OANDA_ACCOUNT_ID", ""),
            }
            if not all(action_env.values()):
                raise ValueError("GitHub Actions credentials unavailable")
            existing = account_environment(Path(root), environ=action_env)
        else:
            existing = account_environment(Path(root), environ={})
        # config.py loads process env at import; set these values only in
        # this dedicated short-lived Python process, before constructing
        # the GET-only PracticeTransport.
        config.API_KEY = existing["OANDA_API_TOKEN"]
        config.ACCOUNT_ID = existing["OANDA_ACCOUNT_ID"]

        from broker.practice_readonly_probe import PracticeReadOnlyProbe
        report = PracticeReadOnlyProbe(
            expected_account_id=EXPECTED_ACCOUNT,
        ).run()
        if (report.source != "OANDA_PRACTICE_NETWORK"
            or report.environment != "PRACTICE"
            or not report.account_read_verified
            or not report.summary_read_verified
            or not report.instrument_catalog_verified
            or report.broker_writes_enabled):
            raise ValueError("Practice network evidence not verified")
    except Exception:
        # No exception text: HTTP errors may contain request URLs/headers.
        print(json.dumps({
            "status": "AUTHENTICATED_PRACTICE_UNVERIFIED",
            "broker_mutations": 0,
        }, sort_keys=True))
        return 3

    print(json.dumps({
        "status": "AUTHENTICATED_PRACTICE_VERIFIED",
        "report": report.public(), "broker_mutations": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
