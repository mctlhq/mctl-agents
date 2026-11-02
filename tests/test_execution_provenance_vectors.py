"""Parity test for the vendored mctl-api execution-provenance vectors.

Upstream: mctlhq/mctl-api,
``internal/executionprovenance/testdata/execution_attempt_vectors.json``,
vendored at the merge of mctl-api PR #915 (commit
4980d8a727b2944755031942f12a1c86a03cabce).

NEVER edit the vendored file. To change it, re-vendor the exact bytes from
mctl-api (``gh api ... -H "Accept: application/vnd.github.raw"``) and update
``PINNED`` in the same commit. A ``contract_version`` change needs both.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from orchestrator import execution_provenance as ep

ROOT = Path(__file__).parent.parent
VENDORED = Path(__file__).parent / "fixtures/execution_provenance/execution_attempt_vectors.json"
PINNED = ("execution_attempt_v1", "9ab9cf04a5542004f5e851a0f32b87681249d3e6f7027386945ebe76a46ad24c")
HYPHENATED_WE = re.compile(r"we_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
REQUIRED = (
    "name",
    "engine",
    "engine_ref",
    "root_execution_id",
    "attempt_canonical",
    "attempt_id",
    "lineage_canonical",
    "lineage_root",
)


def check_vector(v: dict[str, Any]) -> None:
    name = v["name"]
    got = {
        "attempt_canonical": ep.attempt_canonical(v["engine"], v["engine_ref"]).decode(),
        "attempt_id": ep.attempt_id(v["engine"], v["engine_ref"]),
        "lineage_canonical": ep.lineage_canonical(v["root_execution_id"]).decode(),
        "lineage_root": ep.lineage_root(v["root_execution_id"]),
    }
    for field, value in got.items():
        assert value == v[field], f"vector {name!r}: {field} mismatch (got {value!r}, recorded {v[field]!r})"
    assert ep.ATTEMPT_ID_RE.fullmatch(v["attempt_id"]), f"vector {name!r}: attempt_id has bad shape"
    assert ep.LINEAGE_ROOT_RE.fullmatch(v["lineage_root"]), f"vector {name!r}: lineage_root has bad shape"


def verify_vendored(raw: bytes, pinned: tuple[str, str] = PINNED) -> None:
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == pinned[1], (
        f"vendored bytes diverge from mctl-api (got {digest}, pinned {pinned[1]}); re-vendor, never edit"
    )
    doc = json.loads(raw)
    version = doc.get("contract_version")
    assert version == pinned[0] == ep.CONTRACT_VERSION, (
        f"contract_version changed ({version!r}): re-vendor from mctl-api and pin the new digest "
        "published in docs/execution-provenance.md"
    )
    vectors = doc.get("vectors")
    assert isinstance(vectors, list) and vectors, "vectors must be a non-empty list"
    for i, v in enumerate(vectors):
        assert isinstance(v, dict), f"vector #{i} is not an object"
        for field in REQUIRED:
            assert isinstance(v.get(field), str), f"vector #{i}: field {field!r} missing or not a string"
    for v in vectors:
        check_vector(v)
    assert any(HYPHENATED_WE.fullmatch(v["root_execution_id"]) for v in vectors), (
        "no vector has a hyphenated we_<uuid> root_execution_id"
    )


def _vectors() -> list[dict[str, Any]]:
    return json.loads(VENDORED.read_bytes())["vectors"]


def _repin(raw: bytes) -> tuple[str, str]:
    return (PINNED[0], hashlib.sha256(raw).hexdigest())


def test_vendored_file_is_canonical() -> None:
    verify_vendored(VENDORED.read_bytes())


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: v["name"])
def test_every_vector_recomputes(vector: dict[str, Any]) -> None:
    check_vector(vector)


def test_hyphenated_we_root_present() -> None:
    assert any(HYPHENATED_WE.fullmatch(v["root_execution_id"]) for v in _vectors())


def test_one_byte_change_with_valid_ids_is_refused() -> None:
    mutated = VENDORED.read_bytes() + b"\n"
    json.loads(mutated)  # still valid JSON, every id still valid
    with pytest.raises(AssertionError, match="vendored bytes diverge"):
        verify_vendored(mutated)


def test_wrong_derived_id_is_refused() -> None:
    raw = VENDORED.read_bytes()
    doc = json.loads(raw)
    first = doc["vectors"][0]
    old = first["attempt_id"]
    first["attempt_id"] = old[:-1] + ("0" if old[-1] != "0" else "1")
    mutated = json.dumps(doc, indent=2).encode()
    with pytest.raises(AssertionError, match=re.escape(first["name"])):
        verify_vendored(mutated, _repin(mutated))


def test_contract_version_bump_is_refused() -> None:
    doc = json.loads(VENDORED.read_bytes())
    doc["contract_version"] = "execution_attempt_v2"
    mutated = json.dumps(doc, indent=2).encode()
    with pytest.raises(AssertionError, match="re-vendor from mctl-api"):
        verify_vendored(mutated, _repin(mutated))


def test_empty_vectors_is_refused() -> None:
    doc = json.loads(VENDORED.read_bytes())
    doc["vectors"] = []
    mutated = json.dumps(doc, indent=2).encode()
    with pytest.raises(AssertionError, match="non-empty"):
        verify_vendored(mutated, _repin(mutated))


def test_missing_hyphenated_root_is_refused() -> None:
    doc = json.loads(VENDORED.read_bytes())
    for v in doc["vectors"]:
        v["root_execution_id"] = "we_" + "0123456789abcdef" * 2  # non-hyphenated, derived fields kept valid
        v["lineage_canonical"] = ep.lineage_canonical(v["root_execution_id"]).decode()
        v["lineage_root"] = ep.lineage_root(v["root_execution_id"])
    mutated = json.dumps(doc, indent=2).encode()
    with pytest.raises(AssertionError, match="hyphenated"):
        verify_vendored(mutated, _repin(mutated))


def test_missing_field_is_refused() -> None:
    doc = json.loads(VENDORED.read_bytes())
    del doc["vectors"][0]["lineage_root"]
    mutated = json.dumps(doc, indent=2).encode()
    with pytest.raises(AssertionError, match="missing or not a string"):
        verify_vendored(mutated, _repin(mutated))


def test_module_is_pure() -> None:
    tree = ast.parse((ROOT / "orchestrator/execution_provenance.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "hashlib", "json", "re"}, imported
