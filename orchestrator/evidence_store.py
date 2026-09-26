"""Tier A persistence and retrieval for `ExecutionEvidence` (mctlhq/
mctl-agents#199, ADR 015: docs/adr/015-execution-evidence-contract.md).

Writes the sealed, canonical-JSON document to the gitops agents-state tree —
the only durable store every agent pod already writes to, and the store ADR
009 sec. 6 already names as the `gitops` retention class:

    platform-gitops/agents-state/_evidence/
      <workflow_type>/<temporal_workflow_id>/<attempt>-<evidence_id>.json
      by-trace/<trace_id>/<evidence_id>            # pointer: the record path

Every path is content-addressed and per-record, so two concurrent
executions of the same DevLoop never touch the same file and there is no
shared index to serialize on. This is Tier A only; an mctl-api evidence
store (Tier B) is an explicit follow-up (ADR 015, "What this ADR does not
decide") and is not built here.

`STATE_DIR` resolution mirrors `orchestrator/run_implementer.py`,
`orchestrator/run_shepherd.py` and `orchestrator/run_issue_investigator.py`
exactly, so this module points at the same gitops worktree those drivers
already use without a second env var to keep in sync.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable
from pathlib import Path

from orchestrator import execution_evidence
from orchestrator.context_snapshot import canonical_json

# Same env var and default every other run_* driver already resolves the
# gitops agents-state tree from (run_implementer.py:210,
# run_issue_investigator.py:115, run_shepherd.py:128,
# run_incident_responder.py:49) — deliberately not a second, evidence-only
# variable that could drift from the others.
DEFAULT_STATE_DIR = Path(os.getenv("STATE_DIR", "/workdir/mctl-gitops/platform-gitops/agents-state"))

EVIDENCE_DIRNAME = "_evidence"
BY_TRACE_DIRNAME = "by-trace"


def evidence_root(state_dir: Path | None = None) -> Path:
    return (state_dir if state_dir is not None else DEFAULT_STATE_DIR) / EVIDENCE_DIRNAME


def record_path(root: Path, *, workflow_type: str, workflow_id: str, attempt: int, evidence_id: str) -> Path:
    return root / (workflow_type or "unknown") / (workflow_id or evidence_id) / f"{attempt}-{evidence_id}.json"


def pointer_path(root: Path, *, trace_id: str, evidence_id: str) -> Path:
    return root / BY_TRACE_DIRNAME / trace_id / evidence_id


def write(record: execution_evidence.ExecutionEvidence, *, state_dir: Path | None = None) -> Path:
    """Persist the sealed document and its `by-trace` pointer. Idempotent:
    re-writing identical inputs reseals to the same `evidence_id` (ADR 015
    sec. 2) and this function does not overwrite an existing file at that
    path — the content is, by construction, already the same bytes."""
    root = evidence_root(state_dir)
    path = record_path(
        root,
        workflow_type=record.execution.workflow_type,
        workflow_id=record.execution.temporal_workflow_id,
        attempt=record.execution.attempt,
        evidence_id=record.evidence_id,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json(record.to_dict())
    if not path.exists():
        path.write_bytes(data)

    trace_id = record.execution.trace_id
    if trace_id:
        pointer = pointer_path(root, trace_id=trace_id, evidence_id=record.evidence_id)
        pointer.parent.mkdir(parents=True, exist_ok=True)
        if not pointer.exists():
            pointer.write_text(str(path), encoding="utf-8")
    return path


def _load(path: Path) -> execution_evidence.ExecutionEvidence:
    text = path.read_text(encoding="utf-8")
    import json

    data = json.loads(text)
    return execution_evidence.ExecutionEvidence.from_dict(data)


def _newest_first(
    records: Iterable[execution_evidence.ExecutionEvidence],
) -> list[execution_evidence.ExecutionEvidence]:
    return sorted(records, key=lambda r: r.execution.attempt, reverse=True)


def read_by_evidence_id(
    evidence_id: str, *, state_dir: Path | None = None,
) -> execution_evidence.ExecutionEvidence | None:
    root = evidence_root(state_dir)
    if not root.exists():
        return None
    for path in root.rglob(f"*-{evidence_id}.json"):
        if path.parent.name == BY_TRACE_DIRNAME or BY_TRACE_DIRNAME in path.parts:
            continue
        return _load(path)
    return None


def read_by_workflow_id(
    workflow_id: str, *, state_dir: Path | None = None,
) -> list[execution_evidence.ExecutionEvidence]:
    root = evidence_root(state_dir)
    if not root.exists():
        return []
    records = []
    for path in root.glob(f"*/{workflow_id}/*.json"):
        try:
            records.append(_load(path))
        except (OSError, ValueError, execution_evidence.ExecutionEvidenceError):
            continue
    return _newest_first(records)


def read_by_trace_id(
    trace_id: str, *, state_dir: Path | None = None,
) -> list[execution_evidence.ExecutionEvidence]:
    root = evidence_root(state_dir)
    pointer_dir = root / BY_TRACE_DIRNAME / trace_id
    if not pointer_dir.exists():
        return []
    records = []
    for pointer in pointer_dir.iterdir():
        if not pointer.is_file():
            continue
        try:
            target = Path(pointer.read_text(encoding="utf-8").strip())
            records.append(_load(target))
        except (OSError, ValueError, execution_evidence.ExecutionEvidenceError):
            continue
    return _newest_first(records)


# ---------------------------------------------------------------------------
# CLI (task 12)
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orchestrator.evidence_store")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="Print the canonical JSON of one or more evidence documents")
    group = show.add_mutually_exclusive_group(required=True)
    group.add_argument("--workflow-id", default="", help="Temporal workflow id")
    group.add_argument("--trace-id", default="", help="Trace id")
    group.add_argument("--evidence-id", default="", help="Evidence id (ev-...)")
    show.add_argument(
        "--state-dir", default=str(DEFAULT_STATE_DIR),
        help="Path to platform-gitops/agents-state/ (defaults to STATE_DIR env)",
    )
    return parser


def _cmd_show(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    if args.evidence_id:
        one = read_by_evidence_id(args.evidence_id, state_dir=state_dir)
        records = [one] if one is not None else []
    elif args.trace_id:
        records = read_by_trace_id(args.trace_id, state_dir=state_dir)
    else:
        records = read_by_workflow_id(args.workflow_id, state_dir=state_dir)

    if not records:
        print("no evidence found", file=sys.stderr)
        return 1

    exit_code = 0
    for record in records:
        if execution_evidence.is_trustworthy(record):
            sys.stdout.buffer.write(canonical_json(record.to_dict()))
            sys.stdout.buffer.write(b"\n")
        else:
            print(
                f"UNTRUSTED: {record.evidence_id} — content_hash does not match a fresh recompute; "
                "not printed as evidence",
                file=sys.stderr,
            )
            exit_code = 1
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.command == "show":
        return _cmd_show(args)
    return 2  # argparse's `required=True` on the subparser makes this unreachable in practice


if __name__ == "__main__":
    raise SystemExit(main())
