#!/usr/bin/env python3
"""Check every agent manifest against its mctl-gitops release binding,
before the release tag exists.

mctlhq/mctl-agents#582 (Refs #470). Since #574 the release promotes an agent
to production only when `check_binding_hash.evaluate_promotion` accepts its
binding. That verdict was first computed in "Refresh agent registry", after
the tag exists and in a step a workflow re-run skips, while the two checks
that run before a release (`binding hash` in pr-validation.yml and
`binding gate` in release-please.yml) covered issue-investigator only. So a
drifted or absent binding for any other agent merged green and was found at
release time, with a manual promotion as the only recovery.

This runs the release's own question ahead of time, for every
`agents/_manifests/*/agent.yaml` on disk:

    uv run --locked python tools/check_agent_bindings.py

- each manifest goes through `evaluate_promotion`, the function
  tools/publish_agent_release.py calls, with the bytes on disk;
- a manifest whose binding is absent (HTTP 404, the contents API's
  documented absence signal) is reported as `missing`, unless the agent is
  listed in `publish_agent_release.UNBOUND_AGENTS`. Whether a refusal fails
  is decided by `publish_agent_release.Outcome.fails_release`, the release's
  own rule, so this cannot be stricter or laxer than the step it predicts;
- issue-investigator additionally keeps its stricter `check()`: that one
  loads the definition through the resolver's `load_definition`, the path a
  declarative investigation takes at run time.

Every agent is evaluated and reported; one bad agent does not hide the
others. The manifest set is read from disk at call time, so the release
gate, which replaces `agents/_manifests` with the release commit's, checks
the agents of that commit, including one this checkout does not have.

Exit codes are `check_binding_hash`'s, with the same meaning:

    0  every manifest matches its binding
    1  at least one binding was observed and is wrong: a mismatch, or a
       missing binding for an agent not listed as unbound
    2  nothing was observed to be wrong, but at least one binding, profile
       or manifest could not be read or validated, or no manifest was found.
       Never a match.

When both happen in one run the exit code is 1. That follows `check()`,
which reports a mismatch it has observed without letting a failed read mask
it: 1 means a re-pin is needed and no re-run clears it, 2 means retry the
read. Both are non-zero, and each agent's own line keeps its own status, so
an unobservable agent is never printed as matching or missing.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
# Run as a plain script from CI, and loaded by path in tests: tools/ is not a
# package, so make its siblings importable either way.
_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import check_binding_hash  # noqa: E402
import publish_agent_release  # noqa: E402
from check_binding_hash import (  # noqa: E402
    EXIT_MATCH,
    EXIT_MISMATCH,
    EXIT_UNOBSERVED,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_MISSING,
    VERDICT_UNOBSERVED,
    resolver,
)

# The stricter single-agent check, reported on its own line: it can disagree
# with evaluate_promotion (it reads a 404 as unobservable, and it validates
# the definition through load_definition), and folding the two into one
# status would hide which of them refused.
RESOLVER_CHECK_LABEL = f"{check_binding_hash.AGENT} (resolver check)"
_RC_STATUS = {EXIT_MATCH: VERDICT_MATCH, EXIT_MISMATCH: VERDICT_MISMATCH, EXIT_UNOBSERVED: VERDICT_UNOBSERVED}
# Statuses that say "observed, and wrong". Everything else that is not a
# match is unknown, and counts as could-not-observe.
_OBSERVED_WRONG = frozenset({VERDICT_MISMATCH, VERDICT_MISSING})


@dataclass(frozen=True)
class Row:
    """One reported line. `fails` is False only for a match, or for a
    refusal the release itself would let pass (a missing binding for an
    agent listed in UNBOUND_AGENTS)."""

    label: str
    status: str
    reason: str
    fails: bool

    @property
    def exit_code(self) -> int:
        if not self.fails:
            return EXIT_MATCH
        return EXIT_MISMATCH if self.status in _OBSERVED_WRONG else EXIT_UNOBSERVED


def manifest_agents() -> list[str]:
    """Every agent with a manifest on disk, as the release lists them from
    the tag: a directory under agents/_manifests holding an agent.yaml."""
    return sorted(p.parent.name for p in resolver.DEFINITIONS_DIR.glob("*/agent.yaml") if p.is_file())


def evaluate(agent: str, transport: httpx.BaseTransport | None = None) -> Row:
    path = resolver.DEFINITIONS_DIR / agent / "agent.yaml"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return Row(agent, VERDICT_UNOBSERVED, f"{path} could not be read: {exc!r}", True)
    try:
        verdict = check_binding_hash.evaluate_promotion(agent, raw, transport)
    except Exception as exc:  # noqa: BLE001 — isolation is the point
        # evaluate_promotion turns every failure it knows into a verdict.
        # Anything it still raises is an evaluation that did not finish:
        # unknown, never a match, and not allowed to hide the other agents.
        return Row(agent, VERDICT_UNOBSERVED, f"the check itself failed, so the result is unknown: {exc!r}", True)
    if verdict.promote:
        return Row(agent, verdict.status, verdict.reason, False)
    outcome = publish_agent_release.Outcome(agent, publish_agent_release.REFUSED, verdict.reason, verdict)
    return Row(agent, verdict.status, verdict.reason, outcome.fails_release)


def exit_status(rows: list[Row]) -> int:
    codes = {row.exit_code for row in rows}
    if EXIT_MISMATCH in codes:
        return EXIT_MISMATCH
    if EXIT_UNOBSERVED in codes:
        return EXIT_UNOBSERVED
    return EXIT_MATCH


def check_all(transport: httpx.BaseTransport | None = None, *, label: str = "") -> int:
    where = f" ({label})" if label else ""
    target = f"{check_binding_hash.GITOPS_REPO}@{check_binding_hash.GITOPS_REF}"

    # Always, whatever the directory listing says: a deleted or unreadable
    # issue-investigator manifest must fail here as it did before.
    resolver_rc = check_binding_hash.check(transport)
    rows = [
        Row(
            RESOLVER_CHECK_LABEL,
            _RC_STATUS.get(resolver_rc, VERDICT_UNOBSERVED),
            f"check_binding_hash.check() exit {resolver_rc}; its own message is above",
            resolver_rc != EXIT_MATCH,
        )
    ]

    agents = manifest_agents()
    rows.extend(evaluate(agent, transport) for agent in agents)
    code = exit_status(rows)
    if not agents:
        # No manifest is not "no drift": the release publishes at least one
        # agent, so an empty listing means this did not look in the right
        # place. An observed mismatch above still outranks it.
        rows.append(
            Row(
                "agents/_manifests",
                VERDICT_UNOBSERVED,
                f"no */agent.yaml under {resolver.DEFINITIONS_DIR}, so no binding was checked",
                True,
            )
        )
        code = exit_status(rows)

    print(f"binding check{where}: {len(agents)} manifest(s) against {target}")
    in_actions = bool(os.environ.get("GITHUB_ACTIONS"))
    for row in rows:
        quiet = row.status != VERDICT_MATCH and not row.fails
        suffix = " (listed in UNBOUND_AGENTS: not a failure)" if quiet else ""
        print(f"  {row.label}: {row.status}{suffix} — {row.reason}")
        if in_actions and row.status != VERDICT_MATCH:
            level = "error" if row.fails else "warning"
            # Annotations are one line.
            print(f"::{level}::{row.label} {row.status}: {row.reason}".replace("\n", " "))
    result = {
        EXIT_MATCH: "every manifest matches its binding",
        EXIT_MISMATCH: "at least one binding is missing or does not match; re-pin or add it "
        f"(see {check_binding_hash.RUNBOOK})",
        EXIT_UNOBSERVED: "nothing was observed to be wrong, but not everything could be read, "
        "and unknown is not a match; re-run once the read works",
    }[code]
    print(f"result{where}: exit {code}: {result}")
    _write_summary(rows, code, result, where, target)
    return code


def _write_summary(rows: list[Row], code: int, result: str, where: str, target: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    lines = [
        f"### Agent bindings{where} against {target}",
        "",
        f"Exit {code}: {result}.",
        "",
        "| agent | status | fails | detail |",
        "|---|---|---|---|",
    ]
    for row in rows:
        detail = row.reason.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{row.label}` | {row.status} | {'yes' if row.fails else 'no'} | {detail} |")
    with open(summary, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument("--label", default="", help="named in the output, e.g. the release commit being checked")
    parser.add_argument(
        "--manifests",
        type=Path,
        help="check this directory of <agent>/agent.yaml instead of agents/_manifests (for trying an edit on a copy)",
    )
    args = parser.parse_args(argv)
    if args.manifests is not None:
        # The resolver's own constant, so check() and evaluate_promotion read
        # the same directory this lists.
        resolver.DEFINITIONS_DIR = args.manifests.resolve()
    return check_all(label=args.label)


if __name__ == "__main__":
    sys.exit(main())
