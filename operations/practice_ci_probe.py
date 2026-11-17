"""Explicit GET-only OANDA Practice evidence from the existing demo .env.example.

Never shell-source, print, copy to GitHub outputs, or modify the user's
credentials. Only the explicit --run mode contacts the broker. Import and
--check are fully offline and cannot turn a skipped probe into a PASS.
"""
from __future__ import annotations

import argparse
import json
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
                       help="Use existing demo file for authenticated GET-only probe")
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
        # The existing safe parser accepts precisely the two intended demo
        # fields. Do not load shell syntax or mix in a partial CI environment.
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
