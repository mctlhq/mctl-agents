"""Verification strategies for a landed security fix, and the planning rule.

Each strategy is a `Verifier`: it looks at one `awaiting_verification`
occurrence whose active attempt has a landed revision (or artifact digest) and
returns an `Outcome`, `NotApplicable`, or `Pending`. Nothing here runs a
scanner or the assurance harness; strategies only consume evidence that
already exists. mctl-api derives the strength from the mechanism and the
recorder's ceiling, so no strength is ever produced here, and `provider_claim`
and `manual` are never recorded.

`plan` is a pure function from strategy outcomes plus the occurrence's own
`verifications[]` read-back to at most one write: which makes a restarted
sweep deterministic.

Trust rule: `untrusted.*` is never parsed (see `contract`); the regression
check is chosen by the PR-reviewed `config/security_regression_tests.json`
only, never by provider-supplied text.
"""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from orchestrator import runtime_assurance as ra
from orchestrator.security_findings import contract

TRANSIENT_RESULTS = frozenset({"unavailable", "error", "timeout"})
DEFINITE_RESULTS = frozenset({"passed", "failed"})

REGRESSION_CONFIG = Path(__file__).resolve().parents[2] / "config" / "security_regression_tests.json"


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


def max_transient() -> int:
    return _env_int("SECURITY_VERIFY_MAX_TRANSIENT", 3)


def deadline_hours() -> int:
    return _env_int("SECURITY_VERIFY_DEADLINE_H", 72)


def window_seconds() -> int:
    return _env_int("SECURITY_VERIFY_WINDOW_S", 3600)


@dataclass(frozen=True)
class Outcome:
    mechanism: str
    result: str
    revision: str = ""
    artifact_digest: str = ""
    evidence_refs: tuple[contract.EvidenceRef, ...] = ()


@dataclass(frozen=True)
class NotApplicable:
    mechanism: str
    reason: str = ""


@dataclass(frozen=True)
class Pending:
    """Applicable, but the evidence has not arrived yet: nothing to record."""

    mechanism: str
    reason: str = ""


@dataclass(frozen=True)
class Context:
    occurrence: contract.Occurrence
    attempt: contract.Attempt
    identity_key: Callable[[], str]
    now: datetime


class Verifier(Protocol):
    mechanism: str

    def verify(self, ctx: Context) -> Outcome | NotApplicable | Pending: ...


def normalize_repository(value: str) -> str:
    v = value.strip().lower().removesuffix(".git").rstrip("/")
    for prefix in ("https://", "http://"):
        v = v.removeprefix(prefix)
    return v.removeprefix("github.com/")


def parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# -- deterministic regression test -----------------------------------------


def load_regression_map(path: Path = REGRESSION_CONFIG) -> dict[tuple[str, str], str]:
    """`{(repository, identity_key): check name}` from the reviewed config.
    A missing or malformed file is an empty map: the strategy is skipped."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out: dict[tuple[str, str], str] = {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        repo, ident, check = entry.get("repository"), entry.get("identity_key"), entry.get("check")
        if isinstance(repo, str) and isinstance(ident, str) and isinstance(check, str) and repo and ident and check:
            out[(normalize_repository(repo), ident)] = check
    return out


CheckRunReader = Callable[[str, str], "list[dict[str, Any]] | None"]


def read_check_runs_gh(repo: str, sha: str) -> list[dict[str, Any]] | None:
    """GitHub check runs on a commit via `gh api`; None when unreadable."""
    from orchestrator import github_token

    github_token.refresh_github_token()
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["gh", "api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100", "--jq", ".check_runs"],  # noqa: S607
            capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode != 0:
            return None
        runs = json.loads(proc.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else None


def _run_id(run: dict[str, Any]) -> int:
    value = run.get("id")
    return value if isinstance(value, int) else 0


@dataclass
class RegressionTestVerifier:
    mapping: dict[tuple[str, str], str] = field(default_factory=dict)
    read_check_runs: CheckRunReader = read_check_runs_gh
    mechanism: str = "deterministic_regression_test"

    def verify(self, ctx: Context) -> Outcome | NotApplicable | Pending:
        repo = normalize_repository(ctx.occurrence.repository)
        sha = ctx.attempt.landed_revision
        if not sha or not any(r == repo for r, _ in self.mapping):
            return NotApplicable(self.mechanism, "no reviewed mapping or no landed revision")
        identity = ctx.identity_key()
        check = self.mapping.get((repo, identity))
        if not check:
            return NotApplicable(self.mechanism, "no reviewed check for this finding")

        def out(result: str) -> Outcome:
            return Outcome(self.mechanism, result, revision=sha)

        runs = self.read_check_runs(repo, sha)
        named = [r for r in runs if r.get("name") == check] if runs is not None else []
        if not named:
            return out("unavailable")
        latest = max(named, key=_run_id)
        if latest.get("status") != "completed":
            return out("unavailable")
        conclusion = latest.get("conclusion")
        if conclusion == "success":
            return out("passed")
        if conclusion == "failure":
            return out("failed")
        if conclusion == "timed_out":
            return out("timeout")
        return out("unavailable")


# -- assurance replay -------------------------------------------------------

#: `(assurance_finding ref) -> (finding dict, replay result dict)`; either may be
#: None. The default knows no store, so the strategy is not applicable.
AssuranceSource = Callable[[contract.EvidenceRef], "tuple[dict[str, Any] | None, dict[str, Any] | None]"]


def no_assurance_source(ref: contract.EvidenceRef) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    return None, None


@dataclass
class AssuranceReplayVerifier:
    source: AssuranceSource = no_assurance_source
    mechanism: str = "assurance_replay"

    def verify(self, ctx: Context) -> Outcome | NotApplicable | Pending:
        landed = ctx.attempt.landed_revision
        if not landed:
            return NotApplicable(self.mechanism, "no landed revision")
        refs = [e for o in ctx.occurrence.observations for e in o.evidence_refs
                if e.kind == contract.EVIDENCE_ASSURANCE_FINDING]
        for ref in refs:
            finding_raw, result_raw = self.source(ref)
            if finding_raw is None:
                continue
            try:
                finding = ra.AssuranceFinding.from_dict(finding_raw)
            except ra.RuntimeAssuranceError:
                continue
            if finding.state != ra.STATE_CONFIRMED:
                continue
            evidence = [
                contract.EvidenceRef(contract.EVIDENCE_ASSURANCE_FINDING, finding.finding_id, finding.content_hash)
            ]
            if result_raw is None:
                # Requesting a replay stays with Runtime Assurance.
                return Outcome(self.mechanism, "unavailable", revision=landed, evidence_refs=tuple(evidence))
            try:
                result = ra.AssuranceResult.from_dict(result_raw)
            except ra.RuntimeAssuranceError:
                continue
            b, f = result.binding, finding.binding
            if (b.scenario, b.runtime) != (f.scenario, f.runtime) or (b.target.kind, b.target.ref) != (
                f.target.kind, f.target.ref,
            ) or b.target.revision != landed:
                continue  # a replay of another scenario, runtime or revision is not evidence here
            evidence.append(contract.EvidenceRef(contract.EVIDENCE_ASSURANCE_RESULT, result.result_id))
            verdict = next(
                (v.verdict for v in result.invariant_verdicts if v.invariant_id == finding.invariant_id), None
            )
            if verdict == ra.VERDICT_HELD and result.outcome == ra.OUTCOME_HELD:
                mapped = "passed"
            elif verdict == ra.VERDICT_VIOLATED and result.outcome == ra.OUTCOME_VIOLATED:
                mapped = "failed"
            else:
                mapped = "unavailable"
            return Outcome(self.mechanism, mapped, revision=landed, evidence_refs=tuple(evidence))
        return NotApplicable(self.mechanism, "no confirmed assurance finding with a bound replay")


# -- scanner rerun ----------------------------------------------------------


def _bound_observations(ctx: Context) -> list[contract.Observation]:
    a = ctx.attempt
    landed_at = parse_time(a.landed_at) if a.landed_at else None
    seen: set[str] = set()
    found: list[contract.Observation] = []
    for o in ctx.occurrence.observations:
        if not o.provider_ref or o.provider_ref.startswith("admin:"):
            continue
        if o.claimed_action not in ("absent", "present"):
            continue
        rev_ok = bool(a.landed_revision) and o.observed_revision == a.landed_revision
        dig_ok = bool(a.landed_artifact_digest) and o.observed_artifact_digest == a.landed_artifact_digest
        if not (rev_ok or dig_ok):
            continue
        at = parse_time(o.observed_at) if o.observed_at else None
        if landed_at is not None and (at is None or at < landed_at):
            continue
        # Redelivery counts once: distinct observation_hash, never response count.
        key = o.observation_hash or o.id
        if key in seen:
            continue
        seen.add(key)
        found.append(o)
    return found


@dataclass
class ScannerRerunVerifier:
    """Reads existing authenticated, bound observations only. A bound `absent`
    is turned into a verification by mctl-api itself, so nothing is recorded
    for it; a bound `present` is `failed`; nothing yet is `Pending`."""

    mechanism: str = "scanner_rerun_exact_revision"

    def verify(self, ctx: Context) -> Outcome | NotApplicable | Pending:
        bound = _bound_observations(ctx)
        if not bound:
            return Pending(self.mechanism, "no authenticated observation bound to the landed revision yet")
        latest = max(bound, key=lambda o: parse_time(o.observed_at) or datetime.min.replace(tzinfo=UTC))
        if latest.claimed_action == "absent":
            return NotApplicable(self.mechanism, "bound absent: recorded server-side by mctl-api")
        a = ctx.attempt
        return Outcome(
            self.mechanism, "failed",
            revision=a.landed_revision if latest.observed_revision == a.landed_revision else "",
            artifact_digest=(
                a.landed_artifact_digest if latest.observed_artifact_digest == a.landed_artifact_digest else ""
            ),
        )


# -- planning ---------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """At most one write: `write` is None for a skip, with the reason."""

    reason: str
    write: Outcome | None = None
    window: int = 0


def locally_bound(outcome: Outcome, attempt: contract.Attempt) -> bool:
    """Every bound field the attempt has set must be carried, equal, by the
    outcome; and the outcome may not carry one the attempt has not set."""
    if outcome.revision != attempt.landed_revision and (outcome.revision or attempt.landed_revision):
        return False
    return not (
        outcome.artifact_digest != attempt.landed_artifact_digest
        and (outcome.artifact_digest or attempt.landed_artifact_digest)
    )


def _run(v: Verifier, ctx: Context) -> Outcome | NotApplicable | Pending:
    try:
        return v.verify(ctx)
    except TimeoutError:
        return Outcome(v.mechanism, "timeout", revision=ctx.attempt.landed_revision,
                       artifact_digest=ctx.attempt.landed_artifact_digest)
    except Exception:  # noqa: BLE001 — a strategy failure is recorded as `error`, never as `passed`
        return Outcome(v.mechanism, "error", revision=ctx.attempt.landed_revision,
                       artifact_digest=ctx.attempt.landed_artifact_digest)


def plan(ctx: Context, verifiers: list[Verifier]) -> Decision:
    a, occ = ctx.attempt, ctx.occurrence
    if not (a.landed_revision or a.landed_artifact_digest):
        return Decision("not_bound")
    window = int(ctx.now.timestamp() // window_seconds())
    window_start = datetime.fromtimestamp(window * window_seconds(), tz=UTC)

    outcomes: list[Outcome] = []
    attempted: list[str] = []
    for v in sorted(verifiers, key=lambda x: contract.strength_rank(x.mechanism)):
        got = _run(v, ctx)
        if isinstance(got, NotApplicable):
            continue
        attempted.append(v.mechanism)
        if isinstance(got, Outcome):
            outcomes.append(got)

    prior = [x for x in occ.verifications if x.attempt_id == a.id]
    chosen = next((o for o in outcomes if o.result in DEFINITE_RESULTS), None)
    if chosen is not None:
        if not locally_bound(chosen, a):
            return Decision("local_not_bound")
        if chosen.result == "passed" and any(
            p.mechanism == chosen.mechanism and p.result == "passed" and p.revision == chosen.revision for p in prior
        ):
            return Decision("already_recorded")
        return Decision("definite", chosen, window)

    # No definite result: exhaustion, from the occurrence's own read-back.
    transient = [p for p in prior if p.result in TRANSIENT_RESULTS and p.mechanism in contract.AGENT_MECHANISMS]
    landed_at = parse_time(a.landed_at) if a.landed_at else None
    overdue = landed_at is not None and ctx.now - landed_at > timedelta(hours=deadline_hours())
    if len(transient) > max_transient() or overdue:
        if any(p.result == "unverifiable" for p in prior):
            return Decision("already_unverifiable")
        mechanism = (attempted or ["scanner_rerun_exact_revision"])[0]
        out = Outcome(mechanism, "unverifiable", revision=a.landed_revision, artifact_digest=a.landed_artifact_digest)
        return Decision("exhausted", out, window)

    if not outcomes:
        return Decision("pending")
    best = outcomes[0]
    if not locally_bound(best, a):
        return Decision("local_not_bound")
    for p in prior:
        recorded = parse_time(p.recorded_at) if p.recorded_at else None
        if (p.mechanism, p.result, p.revision) == (best.mechanism, best.result, best.revision) and (
            recorded is not None and recorded >= window_start
        ):
            return Decision("recorded_this_window")
    return Decision("transient", best, window)
