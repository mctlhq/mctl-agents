#!/usr/bin/env python3
"""Temporal patch-marker scanner, version MS-4 (mctl-agents#1007, P-6).

Decides, for every workflow matched by a visibility query (default: running
DevLoopWorkflow), whether its history contains one patch marker, with THREE
states. The patch id defaults to `deferred-resume-contract`
(DEFERRED_RESUME_PATCH in orchestrator/temporal/workflows/dev_loop.py) and is
chosen with --patch-id; one run scans one patch id and the text and JSON
reports name it, so reports for several ids are separate runs:

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

Which run to look in. The chain walk above is right for a patch that every run
of a watch records, such as the default id. A patch that is recorded only in the
FIRST run of a watch, and not in continue-as-new hops, is looked for in that
first run, and the chain walk reaches it through `continued_from`. The upcoming
id `merge-watch-terminal-reconcile` is of this kind: it is recorded once per
fresh watch history and not on hopped runs. No build carries it yet, so today
every history scanned for it is MARKER_ABSENT by construction. That is a
point-in-time result about what has been recorded so far, not evidence that a
future build is safe to roll out; read it together with the UNKNOWN count and
the listing state.

Every UNKNOWN carries a stable `reason` code, and the summary counts them. An
ancestor the server answers NOT_FOUND for is `retention_gap`: namespace retention
deleted a closed run, so the chain can no longer be read. It stays UNKNOWN and it
BLOCKS, because the deleted run may have recorded the marker. The namespace
retention is reported beside it (informational, its failure does not change a
verdict).

An empty listing is reported as total=0 (exit 0) with the query printed in the
summary, so an over-narrow query is visible; the tool cannot tell it from a
genuinely idle namespace. The listing is checked against the visibility count
before and after the walk: a failed count or any difference is an incomplete
listing and blocks, as does reaching --max-workflows. Churn during a scan looks
the same, and the answer to it is to run the scan again.

Connection: plaintext by default. --tls, the --tls-* files and --api-key-env
configure TLS, mTLS and an API key. An API key is taken from the environment
variable NAMED by --api-key-env, never from argv, requires --tls, and is scrubbed
from everything the tool prints. Histories hold customer data: the tool prints
identifiers and verdicts, never payloads.

Usage:
  python tools/temporal_marker_scan.py --address HOST:7233 --namespace NS \\
      [--patch-id ID] \\
      [--query 'WorkflowType = "DevLoopWorkflow" AND ExecutionStatus = "Running"'] \\
      [--tls] [--tls-ca-file F] [--tls-cert-file F --tls-key-file F] \\
      [--tls-server-name NAME] [--api-key-env VAR] [--max-workflows N] \\
      [--json] [--allow-present]

Exit codes:
  0  every scanned workflow is MARKER_ABSENT and the listing completed
  2  BLOCKING: any UNKNOWN, or the listing / a fetch failed (a failed listing
     page after earlier successes is a failed scan, not a shorter list)
  3  at least one MARKER_PRESENT and no UNKNOWN (informational;
     --allow-present turns it into 0)

The tool is read-only: it only lists and counts workflows, fetches histories and
describes the namespace. It never signals, updates, terminates or starts anything.

Detector acceptance (AGENTS.md): tests/test_temporal_marker_scan.py proves it
green on converged input and red under deliberate mutations of this file.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

VERSION = "MS-4"
MAX_CHAIN_DEPTH = 100
PATCH = "deferred-resume-contract"
DEFAULT_QUERY = 'WorkflowType = "DevLoopWorkflow" AND ExecutionStatus = "Running"'
PRESENT, ABSENT, UNKNOWN = "MARKER_PRESENT", "MARKER_ABSENT", "UNKNOWN"
EXIT_OK, EXIT_BLOCKING, EXIT_PRESENT = 0, 2, 3
DEFAULT_MAX_WORKFLOWS = 1000
_STARTED = "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED"
# Stable reason codes of an UNKNOWN verdict.
R_NO_HISTORY = "no_history"
R_CORRUPT = "corrupt_history"
R_INCOMPLETE = "incomplete_history"
R_MARKER = "undecodable_marker"
R_FETCH = "fetch_failed"
R_RETENTION = "retention_gap"
R_ANCESTOR = "ancestor_unreadable"
R_DEPTH = "chain_too_deep"
R_LOOP = "chain_loop"


@dataclass(frozen=True)
class Verdict:
    verdict: str
    detail: str
    continued_from: str | None = None
    reason: str = ""

    def __getitem__(self, i: int) -> str:  # (verdict, detail) tuple compatibility
        return (self.verdict, self.detail)[i]


def _continued_from(first: Any) -> str | None:
    if not isinstance(first, dict):
        return None
    attrs = first.get("workflowExecutionStartedEventAttributes")
    if not isinstance(attrs, dict):
        return None
    return attrs.get("continuedExecutionRunId") or None


def _not_found(exc: BaseException) -> bool:
    """The server's documented absence signal, not any other failure to read."""
    return getattr(getattr(exc, "status", None), "name", None) == "NOT_FOUND"


def classify(raw: str | bytes | None, patch: str = PATCH) -> Verdict:
    """Classify one serialized history (proto JSON, `{"events": [...]}`)."""
    if raw is None:
        return Verdict(UNKNOWN, "no history", reason=R_NO_HISTORY)
    try:
        doc = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - truncated / corrupt body is UNKNOWN
        return Verdict(UNKNOWN, f"unparseable: {type(exc).__name__}", reason=R_CORRUPT)
    ev = doc.get("events") if isinstance(doc, dict) else doc
    if not isinstance(ev, list) or not ev or not all(isinstance(e, dict) for e in ev):
        return Verdict(UNKNOWN, "no events", reason=R_CORRUPT)
    cont = _continued_from(ev[0])
    try:
        ids = [int(e["eventId"]) for e in ev]
    except Exception:  # noqa: BLE001
        return Verdict(UNKNOWN, "event without eventId", cont, R_CORRUPT)
    if ids[0] != 1 or ev[0].get("eventType") != _STARTED:
        return Verdict(UNKNOWN, "history does not start at the started event", cont, R_INCOMPLETE)
    if ids != list(range(1, len(ids) + 1)):
        return Verdict(UNKNOWN, "event id gap or reorder", cont, R_INCOMPLETE)
    undecodable = 0
    for e in ev:
        m = e.get("markerRecordedEventAttributes")
        if not isinstance(m, dict) or m.get("markerName") != "core_patch":
            continue
        try:
            payloads = m["details"]["patch-data"]["payloads"]
        except Exception:  # noqa: BLE001
            payloads = None
        if not payloads or not isinstance(payloads, list):
            undecodable += 1
            continue
        for p in payloads:
            try:
                if json.loads(base64.b64decode(p["data"], validate=True))["id"] == patch:
                    return Verdict(PRESENT, f"event {e['eventId']}", cont)
            except Exception:  # noqa: BLE001
                undecodable += 1
    if undecodable:
        return Verdict(UNKNOWN, f"{undecodable} core_patch marker(s) could not be decoded", cont, R_MARKER)
    return Verdict(ABSENT, f"{len(ev)} events, no {patch}", cont)


@dataclass
class Entry:
    workflow_id: str
    run_id: str
    workflow_type: str
    verdict: str
    detail: str
    continued_from: str | None = None
    reason: str = ""


@dataclass
class ScanResult:
    entries: list[Entry]
    listing_complete: bool
    listing_error: str | None = None
    query: str = ""
    namespace: str = ""
    patch: str = PATCH
    retention_seconds: int | None = None
    retention_error: str | None = None

    def unknown_reasons(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for e in self.entries:
            if e.verdict == UNKNOWN:
                c[e.reason] = c.get(e.reason, 0) + 1
        return dict(sorted(c.items()))

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
CountFn = Callable[[str], Awaitable[int]]


async def _resolve_chain(fetch_fn: FetchFn, wf_id: str, own: Verdict, patch: str) -> Verdict:
    """Walk continue-as-new ancestors of an own-history-ABSENT run. Never raises."""
    if own.verdict != ABSENT or not own.continued_from:
        return own
    seen: set[str] = set()
    prev: str | None = own.continued_from
    depth = 0
    while prev:
        if prev in seen:
            detail = f"continue-as-new chain loops at run {prev}"
            return Verdict(UNKNOWN, detail, own.continued_from, R_LOOP)
        if depth >= MAX_CHAIN_DEPTH:
            detail = f"continue-as-new chain deeper than {MAX_CHAIN_DEPTH}"
            return Verdict(UNKNOWN, detail, own.continued_from, R_DEPTH)
        seen.add(prev)
        depth += 1
        try:
            anc = classify(await fetch_fn(wf_id, prev), patch)
        except Exception as exc:  # noqa: BLE001 - an unreadable ancestor is UNKNOWN, never ABSENT
            if _not_found(exc):
                detail = f"ancestor {prev} at depth {depth} is gone (retention)"
                return Verdict(UNKNOWN, detail, own.continued_from, R_RETENTION)
            detail = f"ancestor {prev} fetch failed: {type(exc).__name__}: {exc}"
            return Verdict(UNKNOWN, detail, own.continued_from, R_ANCESTOR)
        if anc.verdict == PRESENT:
            return Verdict(PRESENT, f"ancestor run {prev}: {anc.detail}", own.continued_from)
        if anc.verdict != ABSENT:
            return Verdict(UNKNOWN, f"ancestor {prev}: {anc.detail}", own.continued_from, R_ANCESTOR)
        prev = anc.continued_from
    return Verdict(ABSENT, f"{own.detail}; {depth} ancestor run(s) also without marker", own.continued_from)


async def scan(
    list_fn: ListFn,
    fetch_fn: FetchFn,
    query: str = DEFAULT_QUERY,
    patch: str = PATCH,
    count_fn: CountFn | None = None,
    max_workflows: int | None = None,
) -> ScanResult:
    """List workflows, fetch each history, classify. Never raises on I/O errors."""
    entries: list[Entry] = []
    expected: int | None = None
    try:
        if count_fn is not None:
            expected = await count_fn(query)
        async for wf_id, run_id, wf_type in list_fn(query):
            if max_workflows is not None and len(entries) >= max_workflows:
                return ScanResult(entries, False, f"stopped at --max-workflows {max_workflows}", query, patch=patch)
            try:
                v = await _resolve_chain(fetch_fn, wf_id, classify(await fetch_fn(wf_id, run_id), patch), patch)
            except Exception as exc:  # noqa: BLE001 - any fetch error is UNKNOWN, never ABSENT
                v = Verdict(UNKNOWN, f"fetch failed: {type(exc).__name__}: {exc}", reason=R_FETCH)
            entries.append(Entry(wf_id, run_id, wf_type, v.verdict, v.detail, v.continued_from, v.reason))
        if count_fn is not None:
            after = await count_fn(query)
            if not expected == after == len(entries):
                return ScanResult(
                    entries,
                    False,
                    f"count mismatch: before={expected} after={after} listed={len(entries)}",
                    query,
                    patch=patch,
                )
    except Exception as e:  # noqa: BLE001 - a failed page is a failed scan, not a shorter list
        return ScanResult(entries, False, f"{type(e).__name__}: {e}", query, patch=patch)
    return ScanResult(entries, True, None, query, patch=patch)


def temporal_adapters(client: Any) -> tuple[ListFn, FetchFn]:
    """Read-only adapters over a temporalio Client (list_workflows + fetch_history only)."""

    async def list_fn(query: str) -> AsyncIterator[Listed]:
        async for w in client.list_workflows(query):
            yield w.id, w.run_id, w.workflow_type

    async def fetch_fn(workflow_id: str, run_id: str) -> str:
        history = await client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history()
        return history.to_json()

    return list_fn, fetch_fn


def temporal_count(client: Any) -> CountFn:
    """Read-only visibility count, the cross-check of the listing."""

    async def count_fn(query: str) -> int:
        return (await client.count_workflows(query)).count

    return count_fn


async def namespace_retention(client: Any, namespace: str) -> int:
    """Read-only DescribeNamespace: the retention of closed runs, in seconds."""
    from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest

    resp = await client.workflow_service.describe_namespace(DescribeNamespaceRequest(namespace=namespace))
    return int(resp.config.workflow_execution_retention_ttl.seconds)


def render_text(result: ScanResult) -> str:
    lines = []
    for e in result.entries:
        extra = f" continued_from={e.continued_from}" if e.continued_from else ""
        lines.append(f"{e.verdict:14} {e.workflow_id} run={e.run_id} type={e.workflow_type} ({e.detail}){extra}")
    c = result.counts()
    if not result.listing_complete:
        lines.append(f"LISTING FAILED: {result.listing_error}")
    lines.append(
        f"summary: scanner={VERSION} patch={result.patch} total={len(result.entries)} present={c[PRESENT]} "
        f"absent={c[ABSENT]} unknown={c[UNKNOWN]} unknown_reasons={result.unknown_reasons()} "
        f"listing_complete={result.listing_complete} namespace={result.namespace!r} "
        f"retention_seconds={result.retention_seconds} query={result.query!r}"
    )
    return "\n".join(lines)


def render_json(result: ScanResult, exit_code: int) -> str:
    return json.dumps(
        {
            "scanner_version": VERSION,
            "patch": result.patch,
            "listing_complete": result.listing_complete,
            "listing_error": result.listing_error,
            "query": result.query,
            "namespace": result.namespace,
            "retention_seconds": result.retention_seconds,
            "retention_error": result.retention_error,
            "counts": result.counts(),
            "unknown_reasons": result.unknown_reasons(),
            "exit_code": exit_code,
            "workflows": [asdict(e) for e in result.entries],
        },
        indent=2,
    )


def _connection_options(args: argparse.Namespace) -> dict[str, Any]:
    """Client.connect options from the flags. Raises ValueError/OSError on a bad setup."""
    opts: dict[str, Any] = {}
    if bool(args.tls_cert_file) != bool(args.tls_key_file):
        raise ValueError("--tls-cert-file and --tls-key-file go together")
    files = args.tls_ca_file or args.tls_cert_file or args.tls_server_name
    if files and not args.tls:
        raise ValueError("--tls-* options need --tls")
    if args.tls:
        opts["tls"] = True
    if files:
        from pathlib import Path

        from temporalio.service import TLSConfig

        read = lambda p: Path(p).read_bytes() if p else None  # noqa: E731
        opts["tls"] = TLSConfig(
            server_root_ca_cert=read(args.tls_ca_file),
            client_cert=read(args.tls_cert_file),
            client_private_key=read(args.tls_key_file),
            domain=args.tls_server_name or None,
        )
    if args.api_key_env:
        if not args.tls:
            raise ValueError("--api-key-env needs --tls: a key must not cross the network in plaintext")
        key = os.environ.get(args.api_key_env)
        if not key:
            raise ValueError(f"environment variable {args.api_key_env} is empty or not set")
        opts["api_key"] = key
    return opts


def _scrub(text: str, secret: str | None) -> str:
    return text.replace(secret, "[REDACTED]") if secret else text


async def _connect(address: str, namespace: str, **options: Any) -> Any:
    from temporalio.client import Client

    return await Client.connect(address, namespace=namespace, identity="temporal-marker-scan", **options)


async def _run(args: argparse.Namespace) -> int:
    key: str | None = None
    try:
        options = _connection_options(args)
        key = options.get("api_key")
        client = await _connect(args.address, args.namespace, **options)
    except Exception as e:  # noqa: BLE001
        result = ScanResult([], False, f"connect failed: {type(e).__name__}: {e}", args.query, patch=args.patch_id)
    else:
        result = await scan(
            *temporal_adapters(client),
            query=args.query,
            patch=args.patch_id,
            count_fn=temporal_count(client),
            max_workflows=args.max_workflows,
        )
        try:
            result.retention_seconds = await namespace_retention(client, args.namespace)
        except Exception as e:  # noqa: BLE001 - context for reading the verdicts, not a verdict
            result.retention_error = f"{type(e).__name__}: {e}"
    result.namespace = args.namespace
    code = result.exit_code(args.allow_present)
    print(_scrub(render_json(result, code) if args.json else render_text(result), key))
    return code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Read-only scan for one Temporal patch marker (default: deferred-resume-contract)."
    )
    p.add_argument("--address", required=True, help="Temporal frontend host:port")
    p.add_argument("--namespace", required=True)
    p.add_argument("--patch-id", default=PATCH, help=f"patch id to look for (default: {PATCH})")
    p.add_argument("--query", default=DEFAULT_QUERY, help="visibility query (default: running DevLoopWorkflow)")
    p.add_argument("--tls", action="store_true", help="connect with TLS (system roots unless --tls-ca-file)")
    p.add_argument("--tls-ca-file", help="PEM file with the server root CA")
    p.add_argument("--tls-cert-file", help="PEM file with the mTLS client certificate")
    p.add_argument("--tls-key-file", help="PEM file with the mTLS client key")
    p.add_argument("--tls-server-name", help="server name to verify, when it differs from the address")
    p.add_argument("--api-key-env", metavar="VAR", help="NAME of the environment variable that holds the API key")
    p.add_argument("--max-workflows", type=int, default=DEFAULT_MAX_WORKFLOWS, help="more than this blocks the scan")
    p.add_argument("--json", action="store_true")
    p.add_argument("--allow-present", action="store_true", help="exit 0 instead of 3 when only PRESENT is found")
    args = p.parse_args(argv)
    if not args.patch_id.strip():
        p.error("--patch-id must not be empty")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
