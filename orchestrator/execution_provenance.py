"""Pure derivation of execution provenance ids (``execution_attempt_v1``).

Follows mctl-api#896 and mctl-api ``docs/execution-provenance.md``. Stdlib only,
no I/O. Parity with the Go implementation is proven by
``tests/test_execution_provenance_vectors.py``.
"""

from __future__ import annotations

import hashlib
import json
import re

CONTRACT_VERSION = "execution_attempt_v1"
ATTEMPT_ID_RE = re.compile(r"at_[0-9a-f]{32}")
LINEAGE_ROOT_RE = re.compile(r"ln_[0-9a-f]{32}")


def _cj(items: list[str]) -> bytes:
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"expected str, got {type(item).__name__}")
    return json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def engine_run(engine_ref: str) -> str:
    """Return ``engine_ref`` without any ``#`` suffix."""
    if not isinstance(engine_ref, str):
        raise TypeError(f"expected str, got {type(engine_ref).__name__}")
    return engine_ref.split("#", 1)[0]


def attempt_canonical(engine: str, engine_ref: str) -> bytes:
    return _cj([CONTRACT_VERSION, "attempt", engine, engine_run(engine_ref)])


def lineage_canonical(root_execution_id: str) -> bytes:
    return _cj([CONTRACT_VERSION, "lineage", root_execution_id])


def attempt_id(engine: str, engine_ref: str) -> str:
    return "at_" + hashlib.sha256(attempt_canonical(engine, engine_ref)).hexdigest()[:32]


def lineage_root(root_execution_id: str) -> str:
    return "ln_" + hashlib.sha256(lineage_canonical(root_execution_id)).hexdigest()[:32]
