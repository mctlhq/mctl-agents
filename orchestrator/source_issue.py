"""Read a proposal's source GitHub issue and say what it implies.

Extracted from `run_shepherd._source_issue_state` (mctl-agents#276,
ADR-005) so a second caller — the Tier 2 implementer's admission gate
(mctl-agents#410) — can ask the same question *before* spending a model
attempt, instead of only after reconcile has already established that no
PR exists for the slug.

Deliberately depends on nothing but `json`, `subprocess` (via
`orchestrator.proc.run_capturing`) — never on `run_shepherd` or
`run_implementer`. `orchestrator.pr_adoption` already imports
`run_shepherd`, so an import in either direction from this module would
build a cycle.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orchestrator.proc import run_capturing

# A caller-supplied `gh api ...` reader has the same shape as this module's
# own default: a list of `gh api` arguments in, a parsed JSON payload out.
# `run_shepherd._source_issue_state` passes its own `_gh_api_json` (which
# refreshes GITHUB_TOKEN and logs the command before running it) so its
# existing tests — which patch `run_shepherd._gh_api_json` — keep working
# unchanged; every other caller gets the plain subprocess default below.
GhApiJson = Callable[[list[str]], Any]


def _default_gh_api_json(args: list[str]) -> Any:
    """Run `gh api ...` and parse stdout as JSON. Empty stdout -> None."""
    proc = run_capturing(["gh", "api", *args])
    out = proc.stdout.strip()
    if not out:
        return None
    return json.loads(out)


@dataclass(frozen=True)
class SourceIssueVerdict:
    """What the source issue says about a proposal.

    `known` and `failure` are deliberately two fields rather than one
    nullable one. Collapsing them loses the distinction between "GitHub
    answered: the issue is open" and "GitHub did not answer", and those want
    opposite handling — see `run_shepherd.SourceIssueVerdict`'s original
    docstring (agy's P1 on PR #279) for why folding them together already
    caused an incident once.

    `linked` is the third outcome the implementer's admission gate needs
    that the shepherd's reconcile sweep does not: `known=False` alone
    folds together "this proposal has no `source:` link at all" (admit
    and run) with "GitHub did not answer" (do not run, do not write).
    `linked=False` means the former; `linked=True, known=False` means the
    latter.
    """

    known: bool
    failure: dict[str, str] | None
    linked: bool = True
    issue_ref: str | None = None
    closed_at: str | None = None
    state_reason: str | None = None


def read_source_issue(
    status_data: dict[str, Any],
    *,
    stage: str,
    gh_api_json: GhApiJson | None = None,
) -> SourceIssueVerdict:
    """Read the proposal's source issue and say what it implies.

    `stage` is threaded into the emitted `failure["stage"]` (`"reconcile"`
    for the shepherd, `"admission"` for the implementer) so triage can tell
    which controller refused the proposal.

    Returns `linked=False` when `.status.yaml` carries no `source` block, a
    partial one (missing `repo` or `issue`), or an explicit `type` other
    than `github_issue` — an absent link is not evidence of staleness, and
    incident-responder proposals legitimately carry none. A `source` block
    with no `type` key at all is treated as an implicit `github_issue`
    (the original, pre-extraction shape written before `type` existed):
    only an explicit, different `type` disqualifies it.

    Returns `linked=True, known=False` when there IS a usable source link
    but GitHub could not be read (transport error, non-2xx, unparseable
    payload). Callers must treat that as "change nothing" — an unreadable
    GitHub is not evidence about the proposal.
    """
    source = status_data.get("source")
    if not isinstance(source, dict):
        return SourceIssueVerdict(known=False, failure=None, linked=False)
    source_type = source.get("type")
    if source_type is not None and source_type != "github_issue":
        return SourceIssueVerdict(known=False, failure=None, linked=False)
    repo = source.get("repo")
    number = source.get("issue")
    if not repo or not number:
        return SourceIssueVerdict(known=False, failure=None, linked=False)

    issue_ref = f"{repo}#{number}"
    gh = gh_api_json or _default_gh_api_json
    try:
        issue = gh([f"repos/{repo}/issues/{number}"])
    except Exception as exc:  # noqa: BLE001 — any gh/JSON failure is unknown
        print(f"warn: could not read source issue {repo}#{number}: {exc}")
        return SourceIssueVerdict(known=False, failure=None, issue_ref=issue_ref)
    if not isinstance(issue, dict) or "state" not in issue:
        # A response we cannot interpret is not an answer. Reading it as
        # "open" would be a guess dressed up as a fact.
        print(f"warn: unreadable source issue payload for {repo}#{number}")
        return SourceIssueVerdict(known=False, failure=None, issue_ref=issue_ref)
    if issue["state"] != "closed":
        return SourceIssueVerdict(known=True, failure=None, issue_ref=issue_ref)

    # state_reason is null on issues closed before GitHub introduced it, and
    # on some API paths. Treat "closed, reason unknown" as completed: the
    # actionable half of the message ("no PR ever existed for this slug, the
    # issue is closed") is true either way, and the operator confirms.
    state_reason = issue.get("state_reason")
    not_planned = state_reason == "not_planned"
    code = "source-not-planned" if not_planned else "source-resolved"
    reason = "not planned" if not_planned else "completed"
    return SourceIssueVerdict(
        known=True,
        issue_ref=issue_ref,
        closed_at=issue.get("closed_at"),
        state_reason=state_reason,
        failure={
            "code": code,
            "stage": stage,
            "message": (
                f"No canonical PR exists for the deterministic result branch, "
                f"and the source issue {repo}#{number} is closed as {reason}. "
                f"The branch is missing because this proposal was abandoned, "
                f"not because a PR was lost. Retire it with a terminal status "
                f"(agents-state/OPERATOR.md) or reopen the issue."
            ),
        },
    )
