"""Required-check probing for the Tier 3 shepherd (mctl-agents#411).

Turns the per-context nodes `run_shepherd._fetch_pr_snapshot` attaches to
`PRSnapshot.check_contexts` into a head-pinned `CIStatus`: which required
checks on the CURRENT head are failing, whether any of them are still
completing, and — for the actionable ones — a bounded, annotation-derived
failure excerpt the Tier 2 implementer can act on directly.

Import-light on purpose (`#149`, pinned by `tests/test_worker_isolation.py`):
no `claude_agent_sdk`, no `orchestrator.run_implementer`, no
`orchestrator.run_shepherd` at module level. `run_shepherd.py` imports THIS
module (not the other way around), so a `PRSnapshot` is consumed here only
through a small structural `Protocol` — never through a real import — to
keep the dependency one-directional.

Classification is deliberately conservative in one direction only: an
unclassifiable failure defaults to "actionable". Handing the implementer a
real defect it cannot fix costs one of five attempts; silently filing a real
defect as "infrastructure" restores the exact silent wedge (mctl-agents#409)
this module exists to remove.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

from orchestrator.github_token import refresh_github_token
from orchestrator.proc import run_capturing

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
# First N annotations per failing check-run. A `mypy`/`ruff` failure is
# usually one or a handful of lines; ten is generous headroom without letting
# one pathological check dominate the bundle.
CI_MAX_ANNOTATIONS = 10
# Per-check excerpt bound, mirroring MAX_NOTES_CHARS's philosophy in
# run_shepherd.py: bounded so one check cannot dominate the follow-up bundle.
CI_MAX_EXCERPT_CHARS = 1500
# `contexts(last:100)` in the GraphQL query run_shepherd._fetch_pr_snapshot
# issues. Returning exactly this many nodes is treated as "might be
# truncated" — see read_required_checks.
_CI_CONTEXTS_PAGE_SIZE = 100

# Conclusions that are never actionable code defects, regardless of excerpt
# content. STARTUP_FAILURE covers a runner that never got the job started.
CI_INFRA_CONCLUSIONS = frozenset({
    "CANCELLED",
    "TIMED_OUT",
    "STALE",
    "ACTION_REQUIRED",
    "SKIPPED",
    "NEUTRAL",
    "STARTUP_FAILURE",
})
# The one conclusion (CheckRun `FAILURE`, and the StatusContext `FAILURE`/
# `ERROR` states normalised onto it) whose excerpt is actually inspected —
# annotation-backed failures are actionable, everything else falls through
# to the infrastructure-signature test.
CI_ACTIONABLE_CONCLUSIONS = frozenset({"FAILURE"})

# Infrastructure signatures: matched only when a FAILURE conclusion carries
# no file-anchored annotations at all (see _classify). Case-insensitive.
_CI_INFRA_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"runner (has )?lost",
        r"runner has received a shutdown signal",
        r"connection reset",
        r"TLS handshake",
        r"\b429\b",
        r"rate limit",
        r"no space left on device",
        r"the operation was cancell?ed",
    )
)


class _PRLike(Protocol):
    """The slice of `PRSnapshot` this module actually reads.

    A structural Protocol, not an import of `run_shepherd.PRSnapshot` —
    `run_shepherd` imports `ci_checks`, so importing back would cycle, and
    the whole point of keeping this module import-light is to not need to.
    """

    number: int
    repo: str
    head_sha: str
    check_contexts: tuple
    required_contexts: tuple


@dataclass(frozen=True)
class CheckBlocker:
    """One required, failing check run/status context on the current head."""

    name: str
    workflow: str | None
    job: str | None
    step: str | None
    conclusion: str
    url: str | None
    run_id: str | None
    head_sha: str
    excerpt: str
    kind: str  # "actionable" | "infrastructure"
    required: bool


@dataclass(frozen=True)
class CIStatus:
    """The shepherd's read of required-check state on `head_sha`.

    `known=False` means the probe failed (network error, malformed
    response, a truncated contexts page) — the caller MUST fail closed on
    merging rather than treat an empty `blockers` as "all clear".
    """

    known: bool
    head_sha: str
    pending: bool
    blockers: tuple[CheckBlocker, ...] = ()

    @property
    def actionable(self) -> tuple[CheckBlocker, ...]:
        return tuple(b for b in self.blockers if b.kind == "actionable")

    @property
    def infrastructure(self) -> tuple[CheckBlocker, ...]:
        return tuple(b for b in self.blockers if b.kind == "infrastructure")


def _required_override_from_env() -> frozenset[str]:
    """`SHEPHERD_CI_REQUIRED_OVERRIDE` — an allowlist of check names treated
    as required when GitHub reports no per-context signal AND the branch
    protection context list is silent on them too. Optional, comma/
    whitespace-separated, defaults to empty (no override)."""
    raw = os.environ.get("SHEPHERD_CI_REQUIRED_OVERRIDE", "")
    return frozenset(n.strip() for n in re.split(r"[,\s]+", raw) if n.strip())


def _status_state_to_conclusion(state: str | None) -> str:
    """Map a `StatusContext.state` onto the same vocabulary CheckRun uses.

    PENDING/EXPECTED are not-yet-concluded (handled as pending by the
    caller); ERROR is folded into FAILURE so both feed `_classify` the same
    way — GitHub draws no meaningful actionable/infra distinction between
    "the check errored" and "the check failed" at the commit-status level.
    """
    s = (state or "").upper()
    if s in ("PENDING", "EXPECTED"):
        return ""
    if s == "ERROR":
        return "FAILURE"
    return s


def _normalize_node(node: dict[str, Any]) -> dict[str, Any]:
    """Flatten a raw `CheckRun`/`StatusContext` GraphQL node into one shape."""
    typename = node.get("__typename")
    commit_oid = node.get("_commit_oid")
    if typename == "CheckRun":
        check_suite = node.get("checkSuite") or {}
        workflow_run = check_suite.get("workflowRun") or {}
        workflow = (workflow_run.get("workflow") or {}).get("name")
        run_id = workflow_run.get("databaseId")
        return {
            "typename": "CheckRun",
            "name": node.get("name") or "",
            "status": (node.get("status") or "").upper(),
            "conclusion": (node.get("conclusion") or "").upper(),
            "url": node.get("detailsUrl") or workflow_run.get("url"),
            "required": node.get("isRequired"),
            "workflow": workflow,
            "run_id": str(run_id) if run_id is not None else None,
            "check_run_id": node.get("databaseId"),
            "title": node.get("title") or "",
            "summary": node.get("summary") or "",
            "commit_oid": commit_oid,
        }
    if typename == "StatusContext":
        state = (node.get("state") or "").upper()
        return {
            "typename": "StatusContext",
            "name": node.get("context") or "",
            "status": "IN_PROGRESS" if state in ("PENDING", "EXPECTED") else "COMPLETED",
            "conclusion": _status_state_to_conclusion(state),
            "url": node.get("targetUrl"),
            "required": node.get("isRequired"),
            "workflow": None,
            "run_id": None,
            "check_run_id": None,
            "title": "",
            "summary": "",
            "commit_oid": commit_oid,
        }
    return {}


def _bound_excerpt(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= CI_MAX_EXCERPT_CHARS:
        return text
    return text[:CI_MAX_EXCERPT_CHARS].rstrip() + " ...(truncated)"


def _render_annotations(annotations: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for a in annotations:
        message = (a.get("message") or "").strip()
        if not message:
            continue
        path = a.get("path") or ""
        start_line = a.get("start_line")
        loc = f"{path}:{start_line}" if path and start_line else path
        lines.append(f"{loc}: {message}" if loc else message)
    return "\n".join(lines)


def _fetch_annotations(repo: str, check_run_id: Any) -> list[dict[str, Any]]:
    """`gh api repos/{repo}/check-runs/{id}/annotations`, bounded.

    Never raises: an API/parse failure degrades to an empty list, which the
    caller falls back on (title/summary/text) rather than failing the whole
    probe over one check's annotations being unavailable.

    `Exception`, not an enumerated tuple, for the same reason
    `read_required_checks` uses one — but the consequence here is worse, not
    better. A missing or non-executable `gh` raises `OSError`, which
    `CalledProcessError` does not cover, and `refresh_github_token()` can fail
    its own way; either one escaping turns a RECOVERABLE "fall back to
    title/summary" into a full probe outage via the caller's outer catch, so
    the PR reads as `known=False` and stops being mergeable at all over one
    check's annotations (mctl-agents#411 review P3).
    """
    try:
        refresh_github_token()
        proc = run_capturing([
            "gh", "api", f"repos/{repo}/check-runs/{check_run_id}/annotations",
        ])
    except Exception as e:  # noqa: BLE001 — deliberate: see the docstring
        print(
            f"warn: ci_checks: annotations fetch failed for check-run "
            f"{check_run_id} ({e}); falling back to check output"
        )
        return []
    try:
        data = json.loads(proc.stdout or "[]")
    except (json.JSONDecodeError, TypeError, ValueError):
        print(
            f"warn: ci_checks: annotations for check-run {check_run_id} "
            f"returned malformed JSON; falling back to check output"
        )
        return []
    if not isinstance(data, list):
        return []
    return data[:CI_MAX_ANNOTATIONS]


def _build_excerpt(repo: str, node: dict[str, Any]) -> tuple[str, bool, str | None]:
    """Return (excerpt, has_annotations, step).

    Prefers file/line-anchored annotations (exactly what a `mypy`/`ruff`
    failure produces); falls back to the check's own title/summary text, or
    a synthetic one-liner for a `StatusContext` (which carries no
    annotations API at all).
    """
    if node["typename"] == "CheckRun" and node["conclusion"] == "FAILURE" and node.get("check_run_id"):
        annotations = _fetch_annotations(repo, node["check_run_id"])
        if annotations:
            step = annotations[0].get("title") or None
            return _bound_excerpt(_render_annotations(annotations)), True, step
    title = node.get("title") or ""
    summary = node.get("summary") or ""
    text = "\n".join(p for p in (title, summary) if p)
    if not text and node["typename"] == "StatusContext":
        text = f"{node['name']}: {node['conclusion'] or 'unknown state'}"
    return _bound_excerpt(text), False, None


def _matches_infra_pattern(text: str) -> bool:
    return any(p.search(text or "") for p in _CI_INFRA_PATTERNS)


def _classify(conclusion: str, excerpt: str, has_annotations: bool) -> str:
    """"actionable" | "infrastructure" for one failing required check.

    Unclassifiable defaults to "actionable" — see module docstring for why
    that direction, not the other one, is the safe default here.
    """
    c = (conclusion or "").upper()
    if c in CI_INFRA_CONCLUSIONS:
        return "infrastructure"
    if c in CI_ACTIONABLE_CONCLUSIONS:
        if has_annotations:
            return "actionable"
        if _matches_infra_pattern(excerpt):
            return "infrastructure"
        return "actionable"
    return "actionable"


def read_required_checks(pr: _PRLike) -> CIStatus:
    """Entry point: derive `CIStatus` from `pr.check_contexts`.

    Never raises — a probe failure (malformed data, an unexpectedly-shaped
    node) degrades to `CIStatus(known=False, ...)` so callers fail closed on
    merging rather than crash the shepherd tick.

    The catch is deliberately `Exception` rather than an enumerated tuple.
    The input is a GraphQL blob whose shape we do not control, walked with
    `.get()` at four levels: a non-dict node, a scalar where `checkSuite`
    should be, or a scalar annotation raises `AttributeError`, and a missing
    or non-executable `gh` inside `_fetch_annotations` raises `OSError` —
    none of which an enumerated tuple caught, so each one crashed the whole
    shepherd tick for every OTHER proposal instead of failing closed on this
    one PR. Enumerating is the wrong shape of guarantee for an untrusted
    blob; "never raises" is the contract callers depend on to fail closed
    (mctl-agents#411 review round 4, agy P2).
    """
    try:
        return _read_required_checks(pr)
    except Exception as e:  # noqa: BLE001 — deliberate: see the "never raises" contract above
        print(
            f"warn: ci_checks: required-check probe failed for "
            f"{getattr(pr, 'repo', '?')}#{getattr(pr, 'number', '?')} "
            f"({type(e).__name__}: {e})"
        )
        return CIStatus(known=False, head_sha=getattr(pr, "head_sha", ""), pending=False, blockers=())


def _read_required_checks(pr: _PRLike) -> CIStatus:
    contexts = list(pr.check_contexts or ())
    if len(contexts) >= _CI_CONTEXTS_PAGE_SIZE:
        # contexts(last:100) may have truncated a repo with more contexts
        # than that. Fail closed for this tick rather than merge on a
        # partial view — see design.md's "Risks and mitigations".
        print(
            f"warn: ci_checks: {getattr(pr, 'repo', '?')}#{getattr(pr, 'number', '?')} "
            f"returned {len(contexts)} check contexts (the "
            f"{_CI_CONTEXTS_PAGE_SIZE}-node page cap); treating the probe as "
            f"unknown rather than merging on a partial view"
        )
        return CIStatus(known=False, head_sha=pr.head_sha, pending=False, blockers=())

    branch_required = set(pr.required_contexts or ())
    required_override = _required_override_from_env()

    pending = False
    blockers: list[CheckBlocker] = []
    for raw in contexts:
        node = _normalize_node(raw)
        if not node:
            continue
        if node["commit_oid"] != pr.head_sha:
            # Observed on an earlier head — never fetched into contexts(...)
            # for the current head in the common case, but defensively
            # discarded here too (unit-testable without a live GraphQL call).
            continue

        name = node["name"]
        if not name:
            continue
        signal = node["required"]
        if signal is not None:
            required = bool(signal)
        else:
            required = name in branch_required or name in required_override

        if node["status"] != "COMPLETED":
            if required:
                pending = True
            continue

        conclusion = node["conclusion"]
        if conclusion in ("", "SUCCESS", "SKIPPED", "NEUTRAL"):
            # Passing, an unrecognised state, or SKIPPED/NEUTRAL — GitHub
            # does not gate required-check mergeability on the latter two,
            # so they must never become blockers even when required.
            continue
        if not required:
            continue  # advisory failure — never gates the merge

        excerpt, has_annotations, step = _build_excerpt(pr.repo, node)
        kind = _classify(conclusion, excerpt, has_annotations)
        blockers.append(
            CheckBlocker(
                name=name,
                workflow=node["workflow"],
                job=(name if node["typename"] == "CheckRun" else None),
                step=step,
                conclusion=conclusion,
                url=node["url"],
                run_id=node["run_id"],
                head_sha=pr.head_sha,
                excerpt=excerpt,
                kind=kind,
                required=required,
            )
        )

    return CIStatus(known=True, head_sha=pr.head_sha, pending=pending, blockers=tuple(blockers))
