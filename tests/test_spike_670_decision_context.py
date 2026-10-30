"""Guards and contract tests for the #670 decision-context spike PR.

A follow-up that adopts or rejects the explainer deletes this file.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPIKE = ROOT / "docs" / "spikes" / "670"
REPORT = ROOT / "docs" / "spikes" / "670-decision-context.md"
FIXTURES = SPIKE / "fixtures"
EXAMPLE = FIXTURES / "702-decision-context.example.md"
SLUG = "issue-702-fix-devloop-a-failed-approval-must-not-p"
TOKENS = ("GO", "LIMITED_GO", "NO_GO")
MAX_FILE_BYTES = 256 * 1024
# A real token is a prefix at a word start followed by a long opaque run;
# words such as `risk-` or `restart_if` must not match.
TOKEN_SHAPE = re.compile(rb"\b(?:ghp_|art_|sk-)[A-Za-z0-9_-]{20,}")

_spec = importlib.util.spec_from_file_location("decision_context_670", SPIKE / "decision_context.py")
assert _spec is not None and _spec.loader is not None
dc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dc)

INVALID = {
    "ungrounded_invariant": "UNGROUNDED",
    "sha_mismatch": "SHA_MISMATCH",
    "unknown_key": "UNKNOWN_KEY",
    "auto_approve_key": "GATE_WAIVER_KEY",
    "missing_failure_mode": "MISSING_FAILURE_MODE",
    "pointer_no_ref": "POINTER_NO_REPO_REF",
}


def _load(path: Path) -> dict:
    return dc.load_artifact(path.read_text())


@pytest.mark.parametrize("name,code", sorted(INVALID.items()))
def test_invalid_fixture_rejected_with_expected_code(name: str, code: str) -> None:
    assert dc.codes(dc.validate(_load(FIXTURES / "invalid" / f"{name}.md"))) == [code]


def test_valid_example_has_no_violations() -> None:
    assert dc.validate(_load(EXAMPLE)) == []


def test_ref_resolver_can_reject() -> None:
    out = dc.validate(_load(EXAMPLE), ref_resolver=lambda p, s, a, b: False)
    assert "UNRESOLVED_REF" in dc.codes(out)


def test_freshness() -> None:
    art = _load(EXAMPLE)
    digest = art["binding"]["proposal_content_digest"]
    sha = art["binding"]["target_repo_sha"]
    assert dc.freshness(art, digest, sha) == "fresh"
    assert dc.freshness(art, "sha256:" + "1" * 64, sha) == "stale"
    assert dc.freshness(art, digest, "2" * 40) == "stale"
    assert dc.freshness(art, None, sha) == "unknown"
    assert dc.freshness(art, digest, None) == "unknown"


def test_example_digest_matches_gitops_copy() -> None:
    root = os.environ.get("MCTL_GITOPS_ROOT")
    if not root:
        pytest.skip("MCTL_GITOPS_ROOT not set")
    d = Path(root) / "platform-gitops" / "agents-state" / "mctl-agents" / "proposals" / SLUG
    if not d.is_dir():
        d = Path(root) / "agents-state" / "mctl-agents" / "proposals" / SLUG
    if not d.is_dir():
        pytest.skip("702 proposal not present")
    assert dc.proposal_digest(d) == _load(EXAMPLE)["binding"]["proposal_content_digest"]


@pytest.mark.parametrize("block", ["subject", "binding", "risk", "sections"])
def test_non_mapping_block_is_rejected(block: str) -> None:
    art = _load(EXAMPLE)
    art[block] = "high"
    assert "BAD_BLOCK" in dc.codes(dc.validate(art))


def test_string_risk_does_not_waive_failure_modes() -> None:
    art = _load(EXAMPLE)
    art["risk"] = "high"
    modes = art["sections"]["failure_modes"]
    art["sections"]["failure_modes"] = [c for c in modes if c.get("mode") != "state_after_approval"]
    assert dc.validate(art) != []


@pytest.mark.parametrize("key", ["auto_approve", "skip_review", "satisfies_gate", "waive_ci"])
def test_gate_waiver_keys_rejected_at_depth(key: str) -> None:
    art = _load(EXAMPLE)
    art["sections"]["why"][0][key] = True
    assert "GATE_WAIVER_KEY" in dc.codes(dc.validate(art))


def test_report_is_draft_or_has_exactly_one_token() -> None:
    text = REPORT.read_text()
    if "STATUS: DRAFT" in text:
        return
    m = re.search(r"^STATUS:\s*(\w+)", text, re.MULTILINE)
    assert m and sum(t == m.group(1) for t in TOKENS) == 1


@pytest.mark.parametrize(
    "text,hit",
    [
        (b"ghp_" + b"a" * 36, True),
        (b"key sk-" + b"Z9" * 12, True),
        (b"art_" + b"0" * 24, True),
        (b"restart_if_restartable and high-risk-review", False),
        (b"task-pattern", False),
    ],
)
def test_token_shape(text: bytes, hit: bool) -> None:
    assert bool(TOKEN_SHAPE.search(text)) is hit


def test_fixtures_small_and_free_of_tokens() -> None:
    for path in SPIKE.rglob("*"):
        if not path.is_file() or path.suffix == ".pyc":
            continue
        data = path.read_bytes()
        assert len(data) < MAX_FILE_BYTES, path
        if path.name == "decision_context.py":
            continue
        assert not TOKEN_SHAPE.search(data), path
