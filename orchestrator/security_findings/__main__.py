"""`python -m orchestrator.security_findings start --occurrence soc_... --issue-url ... --tenant ...`"""
from __future__ import annotations

import argparse
import json
import sys

from orchestrator.security_findings.client import SecurityFindingsClient
from orchestrator.security_findings.remediation import start
from orchestrator.work_context.client import WorkItemClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m orchestrator.security_findings")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("start", help="start a remediation attempt for one occurrence")
    s.add_argument("--occurrence", required=True, help="soc_... occurrence id")
    s.add_argument("--issue-url", required=True, help="the tracking issue the DevLoop will run on")
    s.add_argument("--tenant", required=True)
    args = parser.parse_args(argv)

    client = SecurityFindingsClient()
    outcome = start(
        client, WorkItemClient(), occurrence_id=args.occurrence, issue_url=args.issue_url, tenant=args.tenant
    )
    print(json.dumps(outcome.to_dict(), sort_keys=True))
    # A 403 must be loud: the exit is non-zero whatever the outcome class.
    return outcome.exit_code or (3 if client.forbidden_route else 0)


if __name__ == "__main__":
    sys.exit(main())
