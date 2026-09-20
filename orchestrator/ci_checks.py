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

import dataclasses
import json
import math
import os
import re
import subprocess
import time
from collections.abc import Callable
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

# ---------------------------------------------------------------------------
# Bounded CI-log retrieval (mctl-agents#423).
#
# #411 handed the implementer a check name, a conclusion, a run URL and an
# annotation-derived excerpt bounded to CI_MAX_EXCERPT_CHARS -- but for a
# check whose failure is not annotation-anchored (a cross-platform test
# failure, not a mypy/ruff error) that excerpt is empty or uninformative, and
# nothing stopped the implementer from fetching the real CI log itself,
# inside its own execution envelope, with no bound on size or time. One
# incident (mctlhq/mctl-telegram#652) put ~64.6 KB and an unbounded `gh` read
# inside a 900s budget also expected to cover analysis and code mutation.
#
# fetch_failure_logs() retrieves the log in the SHEPHERD process, before the
# implementer subprocess is forked, so the cost is paid outside the budget it
# used to threaten.
CI_LOG_MAX_CHARS = 8000
CI_LOG_TOTAL_MAX_CHARS = 24000
CI_LOG_MAX_CHECKS = 3


def _positive_seconds(name: str, *, default: float) -> float:
    """Local copy of `orchestrator.options._positive_seconds`'s clamp policy
    (a bad value is loud and harmless, never silent and unbounded).

    Duplicated rather than imported: this module's whole point is staying
    import-light (`#149`, pinned indirectly by
    tests/test_worker_isolation.py's `orchestrator.temporal.worker` check,
    since `run_shepherd` -- which the worker reuses read-only helpers from --
    imports this module). `orchestrator.options` imports `claude_agent_sdk`
    at module scope, so importing it here would be exactly the regression
    this module's docstring warns against.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"warn: ci_checks: {name}={raw!r} is not a number; using {default:g}s")
        return default
    if not (value > 0) or math.isinf(value):
        print(f"warn: ci_checks: {name}={raw!r} is not a positive finite number; using {default:g}s")
        return default
    return value


def _env_flag(name: str, *, default: bool) -> bool:
    """A simple on/off kill switch, defaulting on. `0`/`false`/`no`/`off`
    (case-insensitive) turn it off; anything else (including unset) leaves
    it on."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


# Per-fetch and per-bundle wall-clock bounds. Read once at import time, same
# as every other tunable in this module family -- a shepherd tick is a fresh
# process, so "read once" and "read per tick" coincide.
SHEPHERD_CI_LOG_TIMEOUT_SECONDS = _positive_seconds("SHEPHERD_CI_LOG_TIMEOUT_SECONDS", default=45.0)
SHEPHERD_CI_LOG_BUDGET_SECONDS = _positive_seconds("SHEPHERD_CI_LOG_BUDGET_SECONDS", default=120.0)
# Kill switch: SHEPHERD_CI_LOG_FETCH=0 restores the pre-#423 annotation-only
# shape without a redeploy.
SHEPHERD_CI_LOG_FETCH = _env_flag("SHEPHERD_CI_LOG_FETCH", default=True)

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
    # The Actions JOB id -- for a `CheckRun` node this coincides with the
    # check-run's own `databaseId` (mctl-agents#423 task 1's finding), which
    # is what `gh api repos/{repo}/actions/jobs/{id}/logs` wants. `None` for
    # a `StatusContext` (no annotations/logs API at all) and defaulted so
    # every pre-#423 constructor keeps compiling.
    check_run_id: str | None = None
    # Bounded CI-log evidence (mctl-agents#423), fetched by
    # `fetch_failure_logs()` in the shepherd process -- OUTSIDE the
    # implementer's execution envelope. All defaulted: a blocker that never
    # went through retrieval (fetch skipped, disabled, or budget-exhausted)
    # still constructs and renders exactly as it did pre-#423.
    log_excerpt: str = ""
    log_truncated: bool = False
    log_bytes: int = 0
    # "skipped" (fetch never attempted for this blocker -- fetch disabled,
    # or this blocker was never handed to fetch_failure_logs at all),
    # "skipped-budget" (the per-bundle time/count budget was exhausted before
    # this blocker's turn), "ok", "timeout", or "unavailable" (fetch failed
    # or returned something unusable).
    log_status: str = "skipped"


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
        proc = run_capturing(
            ["gh", "api", f"repos/{repo}/check-runs/{check_run_id}/annotations"],
            # mctl-agents#423: this call had no timeout at all before — the
            # same unbounded-retrieval defect fetch_failure_logs() exists to
            # remove, one layer down. `subprocess.TimeoutExpired` is caught by
            # the broad handler below, same as every other failure mode here.
            timeout=SHEPHERD_CI_LOG_TIMEOUT_SECONDS,
        )
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
                check_run_id=(
                    str(node["check_run_id"])
                    if node.get("check_run_id") is not None
                    else None
                ),
            )
        )

    return CIStatus(known=True, head_sha=pr.head_sha, pending=pending, blockers=tuple(blockers))


# ---------------------------------------------------------------------------
# Bounded CI-log retrieval (mctl-agents#423).
# ---------------------------------------------------------------------------
def _bound_log(text: str, max_chars: int) -> tuple[str, bool]:
    """Head+tail excerpt of `text`, bounded to at most `max_chars`.

    A build log puts the failure at the END, so a head-only truncation (like
    `_bound_excerpt`, built for the much shorter annotation text) would keep
    the setup noise and drop exactly the useful part. The elision marker
    between the two halves states what was removed rather than silently
    stitching them together.
    """
    text = text or ""
    if max_chars <= 0:
        return "", bool(text)
    if len(text) <= max_chars:
        return text, False
    # Fixed, generous budget for the marker text itself so the final result
    # never exceeds max_chars regardless of how large `text` is.
    marker_budget = 40
    content_budget = max(0, max_chars - marker_budget)
    head = content_budget // 2
    tail = content_budget - head
    elided = len(text) - head - tail
    marker = f"\n...({elided} bytes elided)...\n"
    bounded = text[:head] + marker + (text[-tail:] if tail else "")
    return bounded[:max_chars], True


def _fetch_one_log(repo: str, blocker: CheckBlocker) -> tuple[str | None, str]:
    """One bounded `gh` read for one blocker's CI log.

    Primary: `gh api repos/{repo}/actions/jobs/{job_id}/logs`, where
    `job_id` is the CheckRun's own `databaseId` (task 1's finding — it
    coincides with the Actions job id in practice). Fallback:
    `gh run view <run_id> --log-failed`, covering the case where the primary
    route 404s or the blocker never carried a check-run id at all.
    `StatusContext` blockers have neither `check_run_id` nor `run_id` and
    degrade straight to "unavailable" without a `gh` call.

    Never raises: every failure mode (missing/non-executable `gh`, a
    non-zero exit, a timed-out child, malformed/non-UTF8 output) degrades to
    `(None, "timeout" | "unavailable")` so the caller's bundle-level budget
    accounting is the only thing that can stop retrieval early.
    """
    try:
        refresh_github_token()
    except Exception as e:  # noqa: BLE001 — see docstring: never raises
        print(f"warn: ci_checks: could not refresh github token for log fetch ({e})")
        return None, "unavailable"

    if blocker.check_run_id:
        try:
            proc = run_capturing(
                ["gh", "api", f"repos/{repo}/actions/jobs/{blocker.check_run_id}/logs"],
                timeout=SHEPHERD_CI_LOG_TIMEOUT_SECONDS,
            )
            return proc.stdout, "ok"
        except subprocess.TimeoutExpired:
            print(
                f"warn: ci_checks: log fetch timed out for check-run "
                f"{blocker.check_run_id} ({repo})"
            )
            return None, "timeout"
        except Exception as e:  # noqa: BLE001 — see docstring: never raises
            print(
                f"warn: ci_checks: job-scoped log fetch failed for check-run "
                f"{blocker.check_run_id} ({repo}: {e}); trying run-scoped fallback"
            )

    if blocker.run_id:
        try:
            proc = run_capturing(
                ["gh", "run", "view", blocker.run_id, "--log-failed"],
                timeout=SHEPHERD_CI_LOG_TIMEOUT_SECONDS,
            )
            return proc.stdout, "ok"
        except subprocess.TimeoutExpired:
            print(f"warn: ci_checks: log fetch timed out for run {blocker.run_id} ({repo})")
            return None, "timeout"
        except Exception as e:  # noqa: BLE001 — see docstring: never raises
            print(f"warn: ci_checks: run-scoped log fetch failed for run {blocker.run_id} ({repo}: {e})")
            return None, "unavailable"

    return None, "unavailable"


def fetch_failure_logs(
    repo: str,
    blockers: tuple[CheckBlocker, ...],
    *,
    now: Callable[[], float] = time.monotonic,
) -> tuple[CheckBlocker, ...]:
    """Bounded, best-effort CI-log retrieval for one bundle's blockers.

    Run in the shepherd process, BEFORE the implementer subprocess is
    forked (mctl-agents#423) — the cost this used to pay inside the
    implementer's `anyio.fail_after` envelope is now paid here instead,
    where a shepherd tick has no such outer bound of its own.

    Stops early on either bound, whichever comes first: at most
    `CI_LOG_MAX_CHECKS` checks are fetched, and no fetch is STARTED once the
    cumulative wall-clock (per `now`) reaches `SHEPHERD_CI_LOG_BUDGET_SECONDS`.
    Every blocker skipped for either reason is marked `log_status =
    "skipped-budget"` rather than silently left at its input state, so the
    bundle always says why a check has no log evidence.

    The running total of excerpt characters across the whole bundle is kept
    at or under `CI_LOG_TOTAL_MAX_CHARS`: each check's own bound is
    `min(CI_LOG_MAX_CHARS, <remaining total budget>)`, so a handful of large
    logs cannot together blow the bundle-wide cap even though each
    individually fits under the per-check one.

    Never raises — this module's whole `read_required_checks` contract
    (fail closed on the KNOWN/UNKNOWN axis, never crash the tick) extends to
    retrieval: every path through `_fetch_one_log` already degrades to
    `(None, "timeout" | "unavailable")` rather than propagating.

    `SHEPHERD_CI_LOG_FETCH=0` (or an empty `blockers`) returns `blockers`
    completely unchanged — the pre-#423 annotation-only shape.
    """
    if not blockers or not SHEPHERD_CI_LOG_FETCH:
        return blockers

    start = now()
    fetched = 0
    total_chars = 0
    out: list[CheckBlocker] = []
    for blocker in blockers:
        elapsed = now() - start
        remaining_total = CI_LOG_TOTAL_MAX_CHARS - total_chars
        if fetched >= CI_LOG_MAX_CHECKS or elapsed >= SHEPHERD_CI_LOG_BUDGET_SECONDS or remaining_total <= 0:
            out.append(dataclasses.replace(blocker, log_status="skipped-budget"))
            continue

        raw, status = _fetch_one_log(repo, blocker)
        fetched += 1
        if raw is None:
            out.append(dataclasses.replace(blocker, log_status=status))
            continue

        per_check_cap = min(CI_LOG_MAX_CHARS, remaining_total)
        excerpt, truncated = _bound_log(raw, per_check_cap)
        total_chars += len(excerpt)
        out.append(
            dataclasses.replace(
                blocker,
                log_excerpt=excerpt,
                log_truncated=truncated,
                log_bytes=len(raw.encode("utf-8", errors="replace")),
                log_status=status,
            )
        )
    return tuple(out)
