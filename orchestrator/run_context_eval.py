"""Read-only stored-snapshot replay CLI (mctlhq/mctl-agents#526, ADR 015
"Replay CLI"):

    python -m orchestrator.run_context_eval --work-item <id> [--execution we_...] [--json]

Reads one already-sealed `ContextSnapshot` back out of mctl-api and scores it
with `context_eval.evaluate(..., evidence_kind="stored-replay")` — no rerun
of the investigation. It issues no write of any kind: no seal, no POST, no
`.status.yaml` update, no gitops commit — and needs no writer token, the
same "read-only, no writer token" precedent `run_usage_collector.py
--dry-run` already sets. `capability_calls` in the printed record is always
`0` — it describes the assembly being scored, which ran long ago — while
this CLI's own store reads are counted separately as `replay_store_reads`,
so the two are never conflated. The stored document itself is never
printed (`snapshots.py`'s own "never logged" rule for `stored_document`);
only the record is.

`main()` follows `orchestrator/run_usage_collector.py:main()`'s argparse
shape: build the parser, validate, do the work, exit.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from orchestrator import context_eval
from orchestrator.context_snapshot import ContextSnapshot, ContextSnapshotError
from orchestrator.work_context.contract import WORK_ITEM_FOUND, ExecutionRef, WorkItem
from orchestrator.work_context.snapshots import EXECUTION_ID_PREFIX, SNAPSHOT_REPLAYED, StoreRef


class ReplayError(RuntimeError):
    """A named, non-retryable reason the replay could not produce a record.
    Never carries the stored document."""


def _load_catalog_identity(strategy_name: str, strategy_version: str) -> tuple[str, str]:
    """`(content_hash, implementation_hash)` from mctlhq/mctl-agents#472's
    catalog, or `("", "")` when the version cannot be loaded (absent,
    disabled, or a recomputed-hash mismatch) — `orchestrator.context_release`
    is imported here, inside the function body, never at this module's top
    level, matching how `context_assembly` already imports
    `orchestrator.work_context` lazily."""
    try:
        from orchestrator import context_release

        version = context_release.load_version(strategy_name, strategy_version)
    except Exception:  # noqa: BLE001 — an unloadable catalog version is empty identity, not a crash
        return "", ""
    return version.content_hash, version.implementation_hash


def _pick_named_execution(item: WorkItem, requested: str) -> ExecutionRef:
    if not requested.startswith(EXECUTION_ID_PREFIX):
        raise ReplayError(f"--execution must be a {EXECUTION_ID_PREFIX!r}-prefixed id, got {requested!r}")
    for execution in item.executions:
        if execution.execution_id == requested:
            return execution
    # Not in the ledger listing is not necessarily "does not exist": the
    # ledger read and the snapshot route are two different reads. Name it so
    # the snapshot read below can still be attempted and fail with its own
    # reason if there really is nothing there.
    return ExecutionRef(execution_id=requested)


def replay(client: Any, work_item_id: str, *, execution_id: str | None = None) -> dict[str, Any]:
    """Read `work_item_id`'s (or exactly `execution_id`'s) stored snapshot,
    verify both identities, evaluate it, link its outcome, and return the
    printed record as a plain dict (with `replay_store_reads` added).
    Raises `ReplayError` — named reason, never the stored document — for an
    absent work item, a snapshot-less execution, or an undecodable stored
    document."""
    reads = 0
    answer = client.get(work_item_id)
    reads += 1
    if answer.verdict != WORK_ITEM_FOUND or answer.item is None:
        raise ReplayError(f"work item {work_item_id!r}: {answer.verdict} ({answer.reason})")
    item = answer.item

    if execution_id is not None:
        named = _pick_named_execution(item, execution_id)
        named_answer = client.execution_snapshot(work_item_id, named.execution_id)
        reads += 1
        if named_answer.verdict != SNAPSHOT_REPLAYED or named_answer.stored_document is None:
            raise ReplayError(
                f"execution {named.execution_id!r} carries no stored snapshot "
                f"({named_answer.verdict}: {named_answer.reason})"
            )
        execution, snapshot_answer = named, named_answer
    else:
        found: tuple[ExecutionRef, Any] | None = None
        for candidate in reversed(item.executions):
            if not candidate.execution_id.startswith(EXECUTION_ID_PREFIX):
                continue
            attempt = client.execution_snapshot(work_item_id, candidate.execution_id)
            reads += 1
            if attempt.verdict == SNAPSHOT_REPLAYED and attempt.stored_document is not None:
                found = (candidate, attempt)
                break
        if found is None:
            raise ReplayError(f"work item {work_item_id!r}: no execution carries a stored snapshot")
        execution, snapshot_answer = found

    try:
        snapshot = ContextSnapshot.from_dict(snapshot_answer.stored_document)
    except ContextSnapshotError as exc:
        raise ReplayError(f"stored document does not decode into a valid ContextSnapshot: {exc}") from exc

    store_ref = StoreRef(
        work_item_id=work_item_id,
        execution_id=execution.execution_id,
        store_snapshot_id=snapshot_answer.snapshot_id,
        store_content_hash=snapshot_answer.content_hash,
    )
    content_hash, implementation_hash = _load_catalog_identity(snapshot.strategy.name, snapshot.strategy.version)
    outcome = context_eval.link_outcome(work_item_state=item.state, execution_phase=execution.phase)
    record = context_eval.evaluate(
        snapshot,
        observed_at=snapshot.created_at,
        store_ref=store_ref,
        outcome=outcome,
        evidence_kind="stored-replay",
        strategy_content_hash=content_hash,
        strategy_implementation_hash=implementation_hash,
    )
    payload = record.to_log_dict()
    # capability_calls describes the assembly being scored, which ran long
    # ago: it is 0 in every replay, never this CLI's own read count.
    if payload.get("metrics") is not None:
        payload["metrics"]["capability_calls"] = 0
    payload["replay_store_reads"] = reads
    return payload


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Read-only replay of a stored ContextSnapshot's retrieval-quality evaluation."
    )
    ap.add_argument("--work-item", required=True, metavar="ID", help="The work item id (mctl-api's own id).")
    ap.add_argument(
        "--execution", default=None, metavar="we_...",
        help="Evaluate exactly this execution's stored snapshot instead of the newest one.",
    )
    ap.add_argument("--json", action="store_true", help="Print the record alone, with no leading tag.")
    args = ap.parse_args(argv)

    from orchestrator.work_context.client import WorkItemClient

    client = WorkItemClient()
    try:
        payload = replay(client, args.work_item, execution_id=args.execution)
    except ReplayError as exc:
        print(f"context-eval-replay: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    line = json.dumps(payload, sort_keys=True)
    print(line if args.json else f"context_eval={line}")


if __name__ == "__main__":
    main()
