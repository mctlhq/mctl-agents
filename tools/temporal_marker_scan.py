#!/usr/bin/env python3
"""Temporal patch-marker scanner, version MS-2 (mctl-agents#1007, P-6).

Decides, for every workflow matched by a visibility query (default: running
DevLoopWorkflow), whether its history contains the patch marker
`deferred-resume-contract` (DEFERRED_RESUME_PATCH in
orchestrator/temporal/workflows/dev_loop.py), with THREE states:

  MARKER_PRESENT  the history is complete and records a core_patch marker
                  whose id is the patch.
  MARKER_ABSENT   the history is complete (contiguous event ids from 1, first
                  event WorkflowExecutionStarted, every payload decoded) and
                  records no such marker *so far*. A running workflow can only
                  prove absence up to now, never that it will not record it.
  UNKNOWN         anything else: fetch error, empty/corrupt/partial history,
                  id gap, a core_patch payload that cannot be decoded. UNKNOWN
                  is never treated as ABSENT, and it BLOCKS (exit 2).

Continue-as-new: a run that is the product of continue-as-new starts a fresh
history and has no marker of its own, because the contract flag is carried in
the resume payload and never re-evaluated. So a missing marker in such a run
proves nothing. The scan walks the chain back through `continued_from` (the
previous run id, fetched read-only under the same workflow id): PRESENT if any
ancestor records the marker; UNKNOWN if any ancestor fetch fails, is empty or
undecodable, the chain is deeper than MAX_CHAIN_DEPTH (100) or loops; ABSENT
only when the whole chain is ABSENT. `classify()` judges ONE history only; the
chain verdict comes from `scan()`.

An empty listing is reported as total=0 (exit 0) with the query printed in the
summary, so an over-narrow query is visible; the tool cannot tell it from a
genuinely idle namespace.

Usage:
  python tools/temporal_marker_scan.py --address HOST:7233 --namespace NS \\
      [--query 'WorkflowType = "DevLoopWorkflow" AND ExecutionStatus = "Running"'] \\
      [--json] [--allow-present]

Exit codes:
  0  every scanned workflow is MARKER_ABSENT and the listing completed
  2  BLOCKING: any UNKNOWN, or the listing / a fetch failed (a failed listing
     page after earlier successes is a failed scan, not a shorter list)
  3  at least one MARKER_PRESENT and no UNKNOWN (informational;
     --allow-present turns it into 0)

The tool is read-only: it only lists workflows and fetches histories. It never
signals, updates, terminates or starts anything.

Detector acceptance (AGENTS.md): tests/test_temporal_marker_scan.py proves it
green on converged input and red under deliberate mutations of this file.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

VERSION = "MS-3"
MAX_CHAIN_DEPTH = 100
PATCH = "deferred-resume-contract"
DEFAULT_QUERY = 'WorkflowType = "DevLoopWorkflow" AND ExecutionStatus = "Running"'
PRESENT, ABSENT, UNKNOWN = "MARKER_PRESENT", "MARKER_ABSENT", "UNKNOWN"
EXIT_OK, EXIT_BLOCKING, EXIT_PRESENT = 0, 2, 3
_STARTED = "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED"


@dataclass(frozen=True)
class Verdict:
    verdict: str
    detail: str
    continued_from: str | None = None

    def __getitem__(self, i: int) -> str:  # (verdict, detail) tuple compatibility
        return (self.verdict, self.detail)[i]


def _continued_from(first: Any) -> str | None:
    if not isinstance(first, dict):
        return None
    attrs = first.get("workflowExecutionStartedEventAttributes")
    if not isinstance(attrs, dict):
        return None
    return attrs.get("continuedExecutionRunId") or None


def classify(raw: str | bytes | None, patch: str = PATCH) -> Verdict:
    """Classify one serialized history (proto JSON, `{"events": [...]}`)."""
    if raw is None:
        return Verdict(UNKNOWN, "no history")
    try:
        doc = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - truncated / corrupt body is UNKNOWN
        return Verdict(UNKNOWN, f"unparseable: {type(exc).__name__}")
    ev = doc.get("events") if isinstance(doc, dict) else doc
    if not isinstance(ev, list) or not ev or not all(isinstance(e, dict) for e in ev):
        return Verdict(UNKNOWN, "no events")
    cont = _continued_from(ev[0])
    try:
        ids = [int(e["eventId"]) for e in ev]
    except Exception:  # noqa: BLE001
        return Verdict(UNKNOWN, "event without eventId", cont)
    if ids[0] != 1 or ev[0].get("eventType") != _STARTED:
        return Verdict(UNKNOWN, "history does not start at the started event", cont)
    if ids != list(range(1, len(ids) + 1)):
        return Verdict(UNKNOWN, "event id gap or reorder", cont)
    undecodable = 0
    for e in ev:
        m = e.get("markerRecordedEventAttributes")
        if not m or m.get("markerName") != "core_patch":
            continue
        try:
            payloads = m["details"]["patch-data"]["payloads"]
        except Exception:  # noqa: BLE001
            payloads = None
        if not payloads:
            undecodable += 1
            continue
        for p in payloads:
            try:
                if json.loads(base64.b64decode(p["data"], validate=True))["id"] == patch:
                    return Verdict(PRESENT, f"event {e['eventId']}", cont)
            except Exception:  # noqa: BLE001
                undecodable += 1
    if undecodable:
        return Verdict(UNKNOWN, f"{undecodable} core_patch marker(s) could not be decoded", cont)
    return Verdict(ABSENT, f"{len(ev)} events, no {patch}", cont)


@dataclass
class Entry:
    workflow_id: str
    run_id: str
    workflow_type: str
    verdict: str
    detail: str
    continued_from: str | None = None


@dataclass
class ScanResult:
    entries: list[Entry]
    listing_complete: bool
    listing_error: str | None = None
    query: str = ""

    def counts(self) -> dict[str, int]:
        c = {PRESENT: 0, ABSENT: 0, UNKNOWN: 0}
        for e in self.entries:
            c[e.verdict] += 1
        return c

    def exit_code(self, allow_present: bool = False) -> int:
        c = self.counts()
        if not self.listing_complete or c[UNKNOWN]:
            return EXIT_BLOCKING
        if c[PRESENT] and not allow_present:
            return EXIT_PRESENT
        return EXIT_OK


# A listed workflow is (workflow_id, run_id, workflow_type).
Listed = tuple[str, str, str]
ListFn = Callable[[str], AsyncIterator[Listed]]
FetchFn = Callable[[str, str], Awaitable[str | bytes | None]]


async def _resolve_chain(fetch_fn: FetchFn, wf_id: str, own: Verdict, patch: str) -> Verdict:
    """Walk continue-as-new ancestors of an own-history-ABSENT run. Never raises."""
    if own.verdict != ABSENT or not own.continued_from:
        return own
    seen: set[str] = set()
    prev: str | None = own.continued_from
    depth = 0
    while prev:
        if prev in seen:
            return Verdict(UNKNOWN, f"continue-as-new chain loops at run {prev}", own.continued_from)
        if depth >= MAX_CHAIN_DEPTH:
            return Verdict(UNKNOWN, f"continue-as-new chain deeper than {MAX_CHAIN_DEPTH}", own.continued_from)
        seen.add(prev)
        depth += 1
        try:
            anc = classify(await fetch_fn(wf_id, prev), patch)
        except Exception as exc:  # noqa: BLE001 - an unreadable ancestor is UNKNOWN, never ABSENT
            return Verdict(UNKNOWN, f"ancestor {prev} fetch failed: {type(exc).__name__}: {exc}", own.continued_from)
        if anc.verdict == PRESENT:
            return Verdict(PRESENT, f"ancestor run {prev}: {anc.detail}", own.continued_from)
        if anc.verdict != ABSENT:
            return Verdict(UNKNOWN, f"ancestor {prev}: {anc.detail}", own.continued_from)
        prev = anc.continued_from
    return Verdict(ABSENT, f"{own.detail}; {depth} ancestor run(s) also without marker", own.continued_from)


async def scan(list_fn: ListFn, fetch_fn: FetchFn, query: str = DEFAULT_QUERY, patch: str = PATCH) -> ScanResult:
    """List workflows, fetch each history, classify. Never raises on I/O errors."""
    entries: list[Entry] = []
    try:
        async for wf_id, run_id, wf_type in list_fn(query):
            try:
                v = await _resolve_chain(fetch_fn, wf_id, classify(await fetch_fn(wf_id, run_id), patch), patch)
            except Exception as exc:  # noqa: BLE001 - any fetch error is UNKNOWN, never ABSENT
                v = Verdict(UNKNOWN, f"fetch failed: {type(exc).__name__}: {exc}")
            entries.append(Entry(wf_id, run_id, wf_type, v.verdict, v.detail, v.continued_from))
    except Exception as e:  # noqa: BLE001 - a failed page is a failed scan, not a shorter list
        return ScanResult(entries, False, f"{type(e).__name__}: {e}", query)
    return ScanResult(entries, True, None, query)


def temporal_adapters(client: Any) -> tuple[ListFn, FetchFn]:
    """Read-only adapters over a temporalio Client (list_workflows + fetch_history only)."""

    async def list_fn(query: str) -> AsyncIterator[Listed]:
        async for w in client.list_workflows(query):
            yield w.id, w.run_id, w.workflow_type

    async def fetch_fn(workflow_id: str, run_id: str) -> str:
        history = await client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history()
        return history.to_json()

    return list_fn, fetch_fn


def render_text(result: ScanResult) -> str:
    lines = []
    for e in result.entries:
        extra = f" continued_from={e.continued_from}" if e.continued_from else ""
        lines.append(f"{e.verdict:14} {e.workflow_id} run={e.run_id} type={e.workflow_type} ({e.detail}){extra}")
    c = result.counts()
    if not result.listing_complete:
        lines.append(f"LISTING FAILED: {result.listing_error}")
    lines.append(
        f"summary: scanner={VERSION} patch={PATCH} total={len(result.entries)} present={c[PRESENT]} "
        f"absent={c[ABSENT]} unknown={c[UNKNOWN]} listing_complete={result.listing_complete} query={result.query!r}"
    )
    return "\n".join(lines)


def render_json(result: ScanResult, exit_code: int) -> str:
    return json.dumps(
        {
            "scanner_version": VERSION,
            "patch": PATCH,
            "listing_complete": result.listing_complete,
            "listing_error": result.listing_error,
            "query": result.query,
            "counts": result.counts(),
            "exit_code": exit_code,
            "workflows": [asdict(e) for e in result.entries],
        },
        indent=2,
    )


async def _connect(address: str, namespace: str) -> Any:
    from temporalio.client import Client

    return await Client.connect(address, namespace=namespace)


async def _run(args: argparse.Namespace) -> int:
    try:
        client = await _connect(args.address, args.namespace)
    except Exception as e:  # noqa: BLE001
        result = ScanResult([], False, f"connect failed: {type(e).__name__}: {e}", args.query)
    else:
        result = await scan(*temporal_adapters(client), query=args.query)
    code = result.exit_code(args.allow_present)
    print(render_json(result, code) if args.json else render_text(result))
    return code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only scan for the deferred-resume-contract patch marker.")
    p.add_argument("--address", required=True, help="Temporal frontend host:port")
    p.add_argument("--namespace", required=True)
    p.add_argument("--query", default=DEFAULT_QUERY, help="visibility query (default: running DevLoopWorkflow)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--allow-present", action="store_true", help="exit 0 instead of 3 when only PRESENT is found")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
