"""decision-context/v0 validator and freshness check (spike #670).

Fixture-only prototype. The artifact is advisory context for a human approver;
it is not authority. Nothing here creates, decides or consumes an approval, and
nothing in `orchestrator` imports this module. Only `content_digest_of` is
taken from `orchestrator`, so the spike never introduces a second digest
framing.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from orchestrator.proposal_state import PROPOSAL_DIGEST_FILES, content_digest_of  # noqa: E402

SCHEMA = "decision-context/v0"
SECTIONS = (
    "what_changes",
    "why",
    "how_it_works_today",
    "behaviour_after",
    "invariants",
    "interfaces",
    "security_governance",
    "failure_modes",
    "evidence",
    "review_pointers",
    "not_changing",
)
HIGH_SCRUTINY = ("invariants", "security_governance", "failure_modes", "evidence")
REQUIRED_FAILURE_MODES = ("retry", "timeout_partial", "wrong_identity", "state_after_approval")
TOP_KEYS = ("schema", "subject", "binding", "risk", "sections")
SUBJECT_KEYS = ("service", "slug", "issue")
BINDING_KEYS = ("proposal_content_digest", "target_repo", "target_repo_sha", "context_snapshot_hash", "generator")
BINDING_REQUIRED = ("proposal_content_digest", "target_repo", "target_repo_sha")
RISK_KEYS = ("level", "areas")
CLAIM_KEYS = ("text", "kind", "refs", "why", "mode", "not_applicable")
KINDS = ("explanatory", "grounded")
GATE_WAIVER_KEYS = ("skip_review", "auto_approve", "satisfies_gate")

_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REPO_REF = re.compile(r"repo:(?P<path>[^@#\s]+)@(?P<sha>[0-9a-f]{40})#L(?P<a>\d+)-L(?P<b>\d+)")

FRESH, STALE, UNKNOWN = "fresh", "stale", "unknown"

RefResolver = Callable[[str, str, int, int], bool]


def violation(code: str, where: str) -> str:
    return f"{code}:{where}"


def codes(violations: list[str]) -> list[str]:
    return [v.split(":", 1)[0] for v in violations]


def load_artifact(text: str) -> dict[str, Any]:
    """The first fenced yaml block of the Markdown artifact."""
    m = re.search(r"```yaml\n(.*?)\n```", text, re.DOTALL)
    if not m:
        raise ValueError("MISSING_FRONT")
    data = yaml.safe_load(m.group(1))
    if not isinstance(data, dict):
        raise ValueError("BAD_FRONT")
    return data


def proposal_digest(proposal_dir: Path) -> str:
    files = {}
    for n in PROPOSAL_DIGEST_FILES:
        f = proposal_dir / n
        files[n] = f.read_bytes() if f.is_file() else None
    return content_digest_of(files)


def _is_waiver(key: object) -> bool:
    return isinstance(key, str) and (key in GATE_WAIVER_KEYS or key.startswith("waive_"))


def _scan_waivers(node: Any, where: str, out: list[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if _is_waiver(k):
                out.append(violation("GATE_WAIVER_KEY", f"{where}.{k}"))
            _scan_waivers(v, f"{where}.{k}", out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _scan_waivers(v, f"{where}[{i}]", out)


def _unknown(node: Mapping[str, Any], allowed: tuple[str, ...], where: str, out: list[str]) -> None:
    for k in node:
        if k not in allowed and not _is_waiver(k):
            out.append(violation("UNKNOWN_KEY", f"{where}.{k}"))


def _check_ref(ref: object, where: str, sha: object, resolver: RefResolver | None, out: list[str]) -> bool:
    """True when the ref is well formed (it may still be mismatched)."""
    if not isinstance(ref, str):
        out.append(violation("BAD_REF", where))
        return False
    if ref.startswith("repo:"):
        m = _REPO_REF.fullmatch(ref)
        if not m or int(m["a"]) > int(m["b"]) or int(m["a"]) < 1:
            out.append(violation("BAD_REF", where))
            return False
        if m["sha"] != sha:
            out.append(violation("SHA_MISMATCH", where))
        elif resolver is not None and not resolver(m["path"], m["sha"], int(m["a"]), int(m["b"])):
            out.append(violation("UNRESOLVED_REF", where))
        return True
    if ref.startswith("proposal:"):
        if ref[len("proposal:") :] not in PROPOSAL_DIGEST_FILES:
            out.append(violation("BAD_REF", where))
            return False
        return True
    if ref.startswith("evidence:") and ref[len("evidence:") :].strip():
        return True
    out.append(violation("BAD_REF", where))
    return False


def validate(artifact: Mapping[str, Any], ref_resolver: RefResolver | None = None) -> list[str]:
    """Typed violations as `CODE:where` strings; empty means valid."""
    out: list[str] = []
    _scan_waivers(artifact, "$", out)
    _unknown(artifact, TOP_KEYS, "$", out)
    for k in TOP_KEYS:
        if k not in artifact:
            out.append(violation("MISSING_KEY", k))
    if artifact.get("schema") != SCHEMA:
        out.append(violation("BAD_SCHEMA", "schema"))

    # A block that is present but is not a mapping is malformed, not absent:
    # skipping its checks would let e.g. `risk: high` waive the failure modes.
    for k in ("subject", "binding", "risk", "sections"):
        if k in artifact and not isinstance(artifact[k], dict):
            out.append(violation("BAD_BLOCK", k))

    subject = artifact.get("subject")
    if isinstance(subject, dict):
        _unknown(subject, SUBJECT_KEYS, "subject", out)
    binding = artifact.get("binding")
    sha: object = None
    if isinstance(binding, dict):
        _unknown(binding, BINDING_KEYS, "binding", out)
        for k in BINDING_REQUIRED:
            if k not in binding:
                out.append(violation("MISSING_KEY", f"binding.{k}"))
        sha = binding.get("target_repo_sha")
        if not (isinstance(sha, str) and _SHA.fullmatch(sha)):
            out.append(violation("BAD_BINDING", "binding.target_repo_sha"))
        pd = binding.get("proposal_content_digest")
        if not (isinstance(pd, str) and _DIGEST.fullmatch(pd)):
            out.append(violation("BAD_BINDING", "binding.proposal_content_digest"))
    risk = artifact.get("risk")
    level = None
    if isinstance(risk, dict):
        _unknown(risk, RISK_KEYS, "risk", out)
        level = risk.get("level")
        if level not in ("high", "medium", "low"):
            out.append(violation("BAD_RISK", "risk.level"))

    sections = artifact.get("sections")
    if not isinstance(sections, dict):
        return out
    for name in sections:
        if name not in SECTIONS:
            out.append(violation("UNKNOWN_KEY", f"sections.{name}"))
    for name in SECTIONS:
        if name not in sections:
            out.append(violation("MISSING_SECTION", name))
            continue
        claims = sections[name]
        if not isinstance(claims, list):
            out.append(violation("BAD_CLAIM", name))
            continue
        for i, claim in enumerate(claims):
            where = f"{name}[{i}]"
            if not isinstance(claim, dict) or not isinstance(claim.get("text"), str) or not claim["text"].strip():
                out.append(violation("BAD_CLAIM", where))
                continue
            _unknown(claim, CLAIM_KEYS, where, out)
            kind = claim.get("kind")
            if kind not in KINDS:
                out.append(violation("BAD_KIND", where))
            refs = claim.get("refs") or []
            if not isinstance(refs, list):
                out.append(violation("BAD_REF", where))
                refs = []
            well_formed = [_check_ref(r, f"{where}.refs[{j}]", sha, ref_resolver, out) for j, r in enumerate(refs)]
            if name in HIGH_SCRUTINY and (kind != "grounded" or not any(well_formed)):
                out.append(violation("UNGROUNDED", where))
            if name == "review_pointers":
                if not any(isinstance(r, str) and r.startswith("repo:") for r in refs):
                    out.append(violation("POINTER_NO_REPO_REF", where))
                if not (isinstance(claim.get("why"), str) and claim["why"].strip()):
                    out.append(violation("POINTER_NO_WHY", where))
    if level == "high" and isinstance(sections.get("failure_modes"), list):
        present = {c.get("mode") for c in sections["failure_modes"] if isinstance(c, dict)}
        for mode in REQUIRED_FAILURE_MODES:
            if mode not in present:
                out.append(violation("MISSING_FAILURE_MODE", mode))
    return out


def freshness(artifact: Mapping[str, Any], current_digest: str | None, current_sha: str | None) -> str:
    """`fresh`, `stale` or `unknown`. A value that cannot be observed is
    `unknown`, never `fresh`: uncertainty is not authorization."""
    if current_digest is None or current_sha is None:
        return UNKNOWN
    binding = artifact.get("binding")
    if not isinstance(binding, dict):
        return UNKNOWN
    if binding.get("proposal_content_digest") != current_digest or binding.get("target_repo_sha") != current_sha:
        return STALE
    return FRESH
