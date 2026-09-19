"""Discovery and durable record for proposal-less same-repo PR adoption.

mctlhq/mctl-agents#334: some pull requests are opened by hand (or by any tool
that is not ``run_implementer.py``) and therefore have no proposal on disk.
When a gating bot (``run_shepherd.GATING_BOTS``) leaves a blocking P1/P2
finding on such a PR, nothing automated owns the fix loop. This module adds a
second, lightweight durable record beside ``proposals/`` — a ``PRRef`` written
to ``agents-state/<service>/adopted-prs/pr-<number>/.prref.yaml`` — that lets
the existing ``run_shepherd.process_one`` / ``run_implementer --review-feedback``
path drive that PR, FIX_ONLY, never merge.

The whole feature is inert unless ``SHEPHERD_ADOPT_PRS`` is true (or
``--adopt-prs`` is passed): ``discover_adoptable`` short-circuits to ``[]``
before doing any I/O, so importing this module has zero effect on
``run_shepherd``'s default behaviour. See design.md for the full pipeline and
docs/adr/010-lifecycle-ownership-contract.md §10 pilot path 4 for the
ownership shape this plugs into.

``orchestrator.run_shepherd`` is imported at module level (``ProposalRef``,
``FIX_ONLY``, ``_service_mode``, ``_dev_loop_owns_answer``,
``_fetch_pr_snapshot``, ``read_codex_review``, ``_gh_api_json``,
``_service_set_from_env``, ``GATING_BOTS``, ``SHEPHERD_INPUT_STATUSES``).
The reverse import (``run_shepherd`` importing this module) is deliberately
deferred to function scope in ``run_shepherd.py`` — a module-level import
there would cycle.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import SERVICES
from orchestrator import run_shepherd
from orchestrator.lifecycle import policy, rollout
from orchestrator.lifecycle.client import OwnershipClient
from orchestrator.lifecycle.contract import (
    OWNED_BY_ME,
    OWNER_SHEPHERD,
    PHASE_REVIEW_REMEDIATION,
    UNOWNED,
    EntityRef,
    Owner,
)
from orchestrator.proposal_state import load_status, now_iso, update_status_file

ADOPTED_DIRNAME = "adopted-prs"
PRREF_FILENAME = ".prref.yaml"
PRREF_KIND = "pr-ref"
MAX_EVIDENCE = 20
# Same order of magnitude as run_shepherd.MAX_NOTES_CHARS: a finding body is
# agent/reviewer-authored text riding into a durable record, and the evidence
# list is meant to be a pointer back to the finding, not a copy of it.
MAX_FINDING_CHARS = 300

# The implementer's deterministic branch prefix (issue #239's own path). A PR
# on a branch with this prefix belongs there, never here.
AGENTS_BRANCH_PREFIX = "feat/agents-"

_TRUTHY = {"1", "true", "yes", "on"}

# The single actor identity this module ever acquires or reads ownership as.
# Matches the fixture convention in tests/test_lifecycle_reconciler.py.
_SHEPHERD_OWNER = Owner(type=OWNER_SHEPHERD, id="shepherd")


# ---------------------------------------------------------------------------
# Flag surface (task 2). All three default to "nothing changes".
# ---------------------------------------------------------------------------
def adoption_enabled() -> bool:
    """SHEPHERD_ADOPT_PRS — the master switch. Default false."""
    return os.environ.get("SHEPHERD_ADOPT_PRS", "").strip().lower() in _TRUTHY


def adopt_repos() -> frozenset[str]:
    """SHEPHERD_ADOPT_REPOS — the allowlist. Default empty.

    Reused verbatim through ``run_shepherd._service_set_from_env`` so an
    unrecognised name gets the exact same ``warn:`` line
    SHEPHERD_SKIP_SERVICES/SHEPHERD_FIX_ONLY_SERVICES already produce, rather
    than a third copy of that check. Callers must still intersect the result
    with ``SERVICES`` before using it as a query target — this function
    returns exactly what was configured, unknown names included, so the
    warning fires; ``discover_adoptable`` is what makes an unrecognised name
    inert.
    """
    return run_shepherd._service_set_from_env("SHEPHERD_ADOPT_REPOS")


def max_prs_per_tick() -> int:
    """SHEPHERD_ADOPT_MAX_PRS_PER_TICK — bound on records processed per tick.

    Default 1. A non-integer value warns and falls back to 1, mirroring
    ``run_shepherd._settle_min_from_env``'s handling of a malformed env var.
    """
    raw = os.environ.get("SHEPHERD_ADOPT_MAX_PRS_PER_TICK", "1")
    try:
        n = int(raw)
    except ValueError:
        print(
            f"warn: SHEPHERD_ADOPT_MAX_PRS_PER_TICK={raw!r} is not an "
            "integer; defaulting to 1"
        )
        return 1
    return max(n, 0)


# ---------------------------------------------------------------------------
# The record (task 3).
# ---------------------------------------------------------------------------
def slug_for(number: int) -> str:
    return f"pr-{number}"


def record_dir(state_dir: Path, service: str, number: int) -> Path:
    return state_dir / service / ADOPTED_DIRNAME / slug_for(number)


@dataclass
class PRRef(run_shepherd.ProposalRef):
    """Handle to an adopted, proposal-less PR (``.prref.yaml``).

    Subclasses ``ProposalRef`` so ``process_one`` and ``_print_summary``
    consume it unchanged. ``mode`` is forced to ``FIX_ONLY`` at construction —
    regardless of what a caller passes — so ``decide()`` can only ever return
    ``defer-merge`` for it: adoption grants remediation, never merge
    authority (mctlhq/mctl-agents#344).
    """

    repo: str = ""
    number: int = 0
    head_branch: str = ""
    owner_type: str = OWNER_SHEPHERD

    def __post_init__(self) -> None:
        self.status_path = self.proposal_dir / PRREF_FILENAME
        self.mode = run_shepherd.FIX_ONLY
        self.is_adopted = True


def load_prref(path: Path) -> dict[str, Any]:
    return load_status(path)


def write_prref(path: Path, status: str, **fields: Any) -> dict[str, Any]:
    """Write ``.prref.yaml``, skipping the write when nothing would change.

    Same "no gratuitous GitOps commit" rule as
    ``run_shepherd._update_status_if_changed`` — not reused directly because
    that helper takes a live ``ProposalRef`` (``ref.status_path``) and this
    call site (the initial adoption write) has only a bare path.
    """
    existing = load_status(path)
    if existing.get("status", "adopted") == status:
        changed = False
        for key, value in fields.items():
            if value is None:
                if key in existing:
                    changed = True
                    break
            elif existing.get(key) != value:
                changed = True
                break
        if not changed:
            return existing
    return update_status_file(path, status, **fields)


def append_evidence(
    path: Path,
    *,
    at: str,
    repo: str,
    pr: int,
    head_sha: str,
    reviewer: str,
    finding: str,
    attempt: int,
    owner_type: str,
    outcome: str,
) -> None:
    """Append one evidence entry, capped at MAX_EVIDENCE, finding truncated."""
    existing = load_status(path)
    evidence = list(existing.get("evidence") or [])
    evidence.append({
        "at": at,
        "repo": repo,
        "pr": pr,
        "head_sha": head_sha,
        "reviewer": reviewer,
        "finding": (finding or "")[:MAX_FINDING_CHARS],
        "attempt": attempt,
        "owner_type": owner_type,
        "outcome": outcome,
    })
    if len(evidence) > MAX_EVIDENCE:
        evidence = evidence[-MAX_EVIDENCE:]
    update_status_file(path, existing.get("status", "adopted"), evidence=evidence)


# ---------------------------------------------------------------------------
# Ownership and safety gates (task 4). Each is its own function with its own
# refusal reason; none raises out of the discovery pass.
# ---------------------------------------------------------------------------
def _is_fork_raw(node: dict[str, Any], base_owner: str) -> bool:
    """Fork check on a raw GraphQL PR-list node, before ever calling
    ``_fetch_pr_snapshot``. Mirrors the ``is_cross_repository`` rule
    ``_fetch_pr_snapshot`` applies (task 1): ``isCrossRepository`` true, OR
    the head repository owner differs from the base repository owner.
    """
    if node.get("isCrossRepository"):
        return True
    head_owner = (node.get("headRepositoryOwner") or {}).get("login") or ""
    return bool(head_owner) and head_owner != base_owner


def _is_agents_branch(head_branch: str) -> bool:
    """True for the implementer's own deterministic branch (issue #239)."""
    return head_branch.startswith(AGENTS_BRANCH_PREFIX)


def _all_proposal_pr_urls(state_dir: Path) -> frozenset[str]:
    """Every ``pr:`` URL recorded under any ``proposals/*/.status.yaml``.

    One cached pass per ``discover_adoptable`` call — the same glob
    ``run_shepherd._discover_refs`` already walks — so a candidate already
    owned by a proposal is refused without a second read per PR.
    """
    urls: set[str] = set()
    if not state_dir.is_dir():
        return frozenset()
    for service_dir in sorted(state_dir.iterdir()):
        if not service_dir.is_dir() or service_dir.name.startswith("_"):
            continue
        proposals_dir = service_dir / "proposals"
        if not proposals_dir.is_dir():
            continue
        for proposal_dir in sorted(proposals_dir.iterdir()):
            if not proposal_dir.is_dir():
                continue
            try:
                data = load_status(proposal_dir / ".status.yaml")
            except Exception as e:  # noqa: BLE001 — one bad file must not abort discovery
                print(f"warn: {service_dir.name}/{proposal_dir.name}: failed to parse .status.yaml ({e}); skipping")
                continue
            pr_url = data.get("pr")
            if isinstance(pr_url, str) and pr_url:
                urls.add(pr_url)
    return frozenset(urls)


def _owned_by_proposal(pr_url: str, known_proposal_pr_urls: frozenset[str]) -> bool:
    return pr_url in known_proposal_pr_urls


def _closing_issue_numbers(node: dict[str, Any]) -> tuple[int, ...]:
    """Issue numbers off a raw GraphQL PR-list node's ``closingIssuesReferences``
    (GitHub's own "Closes #N" linkage), when ``_OPEN_PRS_QUERY`` requested it.
    Absent/malformed data yields ``()`` — the same "never had a DevLoop"
    answer ``_dev_loop_owns_answer`` already gives a slug with no issue
    number to check.
    """
    raw = ((node.get("closingIssuesReferences") or {}).get("nodes")) or []
    return tuple(n["number"] for n in raw if isinstance(n, dict) and isinstance(n.get("number"), int))


def _devloop_free(
    service: str, slug: str, closing_issue_numbers: tuple[int, ...] = ()
) -> bool:
    """True only on a definite "no live DevLoop". Fails CLOSED on
    LEGACY_UNKNOWN — the opposite of the sweep's fail-open default, because
    adoption is discretionary and an unanswerable probe is not evidence of
    vacancy (unlike the sweep, which already owns the work it is checking).

    ``slug`` here is ``slug_for(number)`` ("pr-<n>") — it never matches
    ``_dev_loop_owns_answer``'s ``issue-(\\d+)-`` probe, so that call alone is
    structurally a no-op for every adopted PR (mctlhq/mctl-agents#334 code
    review): the regex short-circuits to LEGACY_FREE before any liveness
    check runs. ``closing_issue_numbers`` — GitHub's own "Closes #N" linkage
    off the PR, when the discovery query found any — is the only other data
    this module has to resolve a proposal-less PR back to the issue a
    DevLoopWorkflow's id is keyed on. Each linked issue is probed the same
    way the sweep probes a real proposal slug; any one of them being owned or
    unanswerable refuses the whole gate.
    """
    if run_shepherd._dev_loop_owns_answer(service, slug) != run_shepherd.LEGACY_FREE:
        return False
    for n in closing_issue_numbers:
        if run_shepherd._dev_loop_owns_answer(service, f"issue-{n}-adopted-pr") != run_shepherd.LEGACY_FREE:
            return False
    return True


def _mode_permits(service: str) -> bool:
    """False when the service resolves to SKIP — owned end-to-end by another
    PR lifecycle (e.g. pr-steward)."""
    return run_shepherd._service_mode(service) != run_shepherd.SKIP


def _store_permits(entity: EntityRef) -> bool:
    """Consult the ownership store only when the rollout says the new answer
    is computed at all. Admits only UNOWNED / OWNED_BY_ME; an unreachable
    store (UNKNOWN) or a row held by somebody else (OWNED_BY_OTHER) refuses —
    adoption is a discretionary action, so uncertainty must never read as
    vacancy.
    """
    if not rollout.computes_new_answer():
        return True
    answer = OwnershipClient().get(entity, PHASE_REVIEW_REMEDIATION, asking=_SHEPHERD_OWNER)
    return answer.verdict in (UNOWNED, OWNED_BY_ME)


def _refusal_reason(
    *,
    repo: str,
    service: str,
    number: int,
    node: dict[str, Any],
    pr_url: str,
    known_proposal_pr_urls: frozenset[str],
) -> str | None:
    """Every gate but the findings check (which needs a PR snapshot fetch),
    in the cheapest-first order design.md §2 specifies. Returns the refusal
    reason, or None when the candidate clears every gate so far.
    """
    base_owner = repo.split("/")[0]
    if node.get("isDraft"):
        return "draft PR"
    if _is_fork_raw(node, base_owner):
        return "cross-repository (fork) PR"
    head_branch = node.get("headRefName") or ""
    if _is_agents_branch(head_branch):
        return "head branch belongs to the implementer's deterministic prefix"
    if _owned_by_proposal(pr_url, known_proposal_pr_urls):
        return "already owned by an existing proposal"
    if not _devloop_free(service, slug_for(number), _closing_issue_numbers(node)):
        return "a running DevLoopWorkflow may own this entity (fail-closed)"
    if not _mode_permits(service):
        return "service resolves to SKIP (owned by another PR lifecycle)"
    head_sha = node.get("headRefOid") or ""
    entity = EntityRef.for_pull_request(repo, number, head_sha)
    if not _store_permits(entity):
        return "ownership store does not admit this shepherd"
    return None


# ---------------------------------------------------------------------------
# Discovery (task 5).
# ---------------------------------------------------------------------------
_OPEN_PRS_QUERY = (
    "query($owner:String!,$repo:String!){repository(owner:$owner,name:$repo)"
    "{pullRequests(states:OPEN,first:50,orderBy:{field:UPDATED_AT,direction:DESC})"
    "{nodes{number headRefOid headRefName isDraft isCrossRepository "
    "headRepositoryOwner{login} baseRepository{owner{login}} "
    "closingIssuesReferences(first:5){nodes{number}}}}}}"
)


def _list_open_prs(repo: str) -> list[dict[str, Any]]:
    """``gh api graphql`` for a repo's open PRs (candidate set, unpaginated —
    50 open PRs is far past what any of these repos carries at once)."""
    owner, name = repo.split("/", 1)
    view = run_shepherd._gh_api_json([
        "graphql", "-f", f"query={_OPEN_PRS_QUERY}",
        "-F", f"owner={owner}", "-F", f"repo={name}",
    ])
    if not view:
        return []
    nodes = (
        ((view.get("data") or {}).get("repository") or {}).get("pullRequests") or {}
    ).get("nodes") or []
    return [n for n in nodes if isinstance(n, dict)]


def _prref_from_data(service: str, entry: Path, data: dict[str, Any]) -> PRRef | None:
    """Rebuild a PRRef from an on-disk .prref.yaml (re-discovery)."""
    number = data.get("number")
    repo = data.get("repo")
    pr_url = data.get("pr")
    if not isinstance(number, int) or not isinstance(repo, str) or not isinstance(pr_url, str):
        print(f"warn: {service}/{entry.name}: malformed {PRREF_FILENAME} (missing repo/number/pr); skipping")
        return None
    return PRRef(
        service=service,
        slug=slug_for(number),
        proposal_dir=entry,
        status=str(data.get("status", "adopted")),
        review_attempts=int(data.get("review_attempts", 0) or 0),
        harness_failures=int(data.get("harness_failures", 0) or 0),
        refusals=int(data.get("refusals", 0) or 0),
        refusals_head=(data.get("refusals_head") or None),
        pr_url=pr_url,
        repo=repo,
        number=number,
        head_branch=str(data.get("head_branch") or ""),
        owner_type=str(data.get("owner_type") or OWNER_SHEPHERD),
    )


def _iter_prref_data(
    state_dir: Path, repos: frozenset[str]
) -> Iterator[tuple[str, Path, dict[str, Any]]]:
    """Yield ``(service, entry, data)`` for every parseable ``.prref.yaml``
    under any allowlisted service's ``adopted-prs/``, terminal or not. Shared
    walk behind ``_existing_records`` (non-terminal only) and
    ``_all_adopted_pr_urls`` (every record) so the two never drift on what
    counts as "on disk".
    """
    for service in sorted(repos & set(SERVICES)):
        adopted_dir = state_dir / service / ADOPTED_DIRNAME
        if not adopted_dir.is_dir():
            continue
        for entry in sorted(adopted_dir.iterdir()):
            if not entry.is_dir():
                continue
            path = entry / PRREF_FILENAME
            if not path.exists():
                continue
            try:
                data = load_prref(path)
            except Exception as e:  # noqa: BLE001 — one bad record must not abort discovery
                print(f"warn: {service}/{entry.name}: failed to parse {PRREF_FILENAME} ({e}); skipping")
                continue
            yield service, entry, data


def _existing_records(state_dir: Path, repos: frozenset[str]) -> list[PRRef]:
    """Re-discover non-terminal .prref.yaml records so a record created in an
    earlier tick keeps its counters — subject to the durability caveat in
    design.md (adopted-prs/** is not staged back to gitops until
    mctlhq/mctl-gitops#1278 lands).
    """
    accepted_statuses = run_shepherd.SHEPHERD_INPUT_STATUSES | {"adopted"}
    refs: list[PRRef] = []
    for service, entry, data in _iter_prref_data(state_dir, repos):
        if data.get("status", "adopted") not in accepted_statuses:
            continue
        ref = _prref_from_data(service, entry, data)
        if ref is not None:
            refs.append(ref)
    return refs


def _all_adopted_pr_urls(state_dir: Path, repos: frozenset[str]) -> frozenset[str]:
    """Every ``pr:`` URL recorded under any ``adopted-prs/*/.prref.yaml``,
    terminal or not.

    ``_existing_records`` filters out terminal records (``review-stuck``,
    ``merged``, ...) because those no longer need driving — but
    ``discover_adoptable``'s re-adoption guard must still see them, or a PR a
    human already parked in ``review-stuck`` gets treated as a brand-new
    candidate on the very next tick and re-adopted with its counters reset
    (mctlhq/mctl-agents#334 code review).
    """
    return frozenset(
        data["pr"]
        for _service, _entry, data in _iter_prref_data(state_dir, repos)
        if isinstance(data.get("pr"), str) and data["pr"]
    )


def _adopt(
    state_dir: Path,
    *,
    repo: str,
    service: str,
    number: int,
    node: dict[str, Any],
    pr_url: str,
    dry_run: bool,
) -> PRRef | None:
    """The findings gate plus the write. Only reached once every cheaper gate
    in ``_refusal_reason`` has already passed.
    """
    pr = run_shepherd._fetch_pr_snapshot(repo, number)
    if pr is None:
        print(f"info: {repo}#{number}: not adoptable (could not fetch PR snapshot)")
        return None
    codex = run_shepherd.read_codex_review(pr)
    findings = codex.fresh_findings_p1_p2(pr.head_sha, pr.head_pushed_at)
    if not findings:
        print(f"info: {repo}#{number}: not adoptable (no fresh P1/P2 finding on the current head)")
        return None

    head_branch = node.get("headRefName") or ""

    if dry_run:
        print(f"[dry-run] would adopt {repo}#{number} (head={pr.head_sha[:8]}, {len(findings)} fresh finding(s))")
        return PRRef(
            service=service,
            slug=slug_for(number),
            proposal_dir=record_dir(state_dir, service, number),
            status="adopted",
            pr_url=pr_url,
            repo=repo,
            number=number,
            head_branch=head_branch,
        )

    entity = EntityRef.for_pull_request(repo, number, pr.head_sha)
    if rollout.records_writes():
        answer = OwnershipClient().acquire(
            entity, PHASE_REVIEW_REMEDIATION, _SHEPHERD_OWNER,
            proposal_ref="", policy_ref=policy.policy_ref_for(service),
        )
        if not answer.may_mutate:
            # Ownership failure never fails the tick — it downgrades to "do
            # not adopt this PR" (ADR-010 §9's fail-safe direction).
            print(
                f"info: {repo}#{number}: not adoptable (ownership acquire "
                f"refused: {answer.reason or answer.verdict})"
            )
            return None

    proposal_dir = record_dir(state_dir, service, number)
    ref = PRRef(
        service=service,
        slug=slug_for(number),
        proposal_dir=proposal_dir,
        status="adopted",
        pr_url=pr_url,
        repo=repo,
        number=number,
        head_branch=head_branch,
    )
    finding = findings[0]
    adopted_at = now_iso()
    write_prref(
        ref.status_path,
        "adopted",
        kind=PRREF_KIND,
        repo=repo,
        number=number,
        pr=pr_url,
        head_sha=pr.head_sha,
        head_branch=head_branch,
        owner_type=OWNER_SHEPHERD,
        policy_ref=policy.policy_ref_for(service),
        review_attempts=0,
        harness_failures=0,
        refusals=0,
        refusals_head=None,
        adopted_at=adopted_at,
    )
    append_evidence(
        ref.status_path,
        at=adopted_at,
        repo=repo,
        pr=number,
        head_sha=pr.head_sha,
        reviewer=finding.author or "",
        finding=finding.body,
        attempt=0,
        owner_type=OWNER_SHEPHERD,
        outcome="adopted",
    )
    print(f"info: adopted {repo}#{number} (head={pr.head_sha[:8]}, {len(findings)} fresh finding(s))")
    return ref


def discover_adoptable(
    state_dir: Path,
    *,
    budget: int | None = None,
    dry_run: bool = False,
) -> list[PRRef]:
    """The whole adoption pipeline for one sweep tick.

    Returns at most ``budget`` (default ``max_prs_per_tick()``) refs: already
    -adopted, non-terminal records first (they keep driving even when the
    tick's fresh-adoption budget is spent), then newly adopted PRs up to the
    remaining slots. A PR failing any gate is skipped with a one-line reason
    on stdout. An unreachable ownership store or a failed `gh` call yields
    zero adoptions for that PR/repo and never raises.
    """
    cap = max_prs_per_tick() if budget is None else max(budget, 0)
    repos = adopt_repos()
    if cap <= 0 or not repos:
        return []

    refs = _existing_records(state_dir, repos)
    if len(refs) >= cap:
        return refs[:cap]

    known_proposal_pr_urls = _all_proposal_pr_urls(state_dir)
    # Every recorded PR, terminal or not (see _all_adopted_pr_urls) — a
    # `refs`-only guard would miss a `review-stuck` record and re-adopt it.
    known_pr_urls = set(_all_adopted_pr_urls(state_dir, repos))

    for service in sorted(repos & set(SERVICES)):
        if len(refs) >= cap:
            break
        repo = f"mctlhq/{service}"
        try:
            candidates = _list_open_prs(repo)
        except Exception as e:  # noqa: BLE001 — one repo's failure must not abort the tick
            print(f"warn: {repo}: failed to list open PRs for adoption ({e}); skipping")
            continue
        for node in candidates:
            if len(refs) >= cap:
                break
            number = node.get("number")
            if not isinstance(number, int):
                continue
            pr_url = f"https://github.com/{repo}/pull/{number}"
            if pr_url in known_pr_urls:
                continue
            reason = _refusal_reason(
                repo=repo, service=service, number=number, node=node,
                pr_url=pr_url, known_proposal_pr_urls=known_proposal_pr_urls,
            )
            if reason:
                print(f"info: {repo}#{number}: not adoptable ({reason})")
                continue
            ref = _adopt(
                state_dir, repo=repo, service=service, number=number,
                node=node, pr_url=pr_url, dry_run=dry_run,
            )
            if ref is not None:
                refs.append(ref)
                known_pr_urls.add(pr_url)
    return refs[:cap]
