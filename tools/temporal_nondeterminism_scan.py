#!/usr/bin/env python3
"""Temporal non-determinism detector (mctl-agents#1007).

A worker that cannot replay a recorded history (for example a pre-M1 image
replaying a history that carries the `deferred-resume-contract` patch marker)
fails the workflow task with `[TMPRL1100] Nondeterminism error: ...`. The run
stays open and the task is retried, so nothing fails loudly. Measured on a
local dev server (Temporal Server 1.32.0, Python SDK 1.34.0) the failure shows
up in exactly these places:

  history  a WORKFLOW_TASK_FAILED event with cause
           WORKFLOW_TASK_FAILED_CAUSE_NON_DETERMINISTIC_ERROR and a failure
           message that starts with `[TMPRL1100] Nondeterminism error`
  server   the counter `failed_workflow_tasks{failure="NonDeterministicError"}`
           (and `service_errors_nondeterministic`)
  worker   a WARN line from the SDK core logger; in the production rehearsal
           the worker stderr was EMPTY, so the log is not a detection path

This module is the in-repo, testable part of the detector. It judges ONE input
at a time, with three states, and never turns "could not read" into "clean":

  NONDETERMINISM  the input records the failure.
  CLEAN           the input was read completely and records no such failure.
                  For a history this means "none recorded so far".
  UNKNOWN         anything else: missing, unparseable, partial or empty input,
                  an id gap in a history, a metrics scrape without the
                  liveness series. UNKNOWN blocks.

The alert rule that would page on the server counter belongs to the platform
(mctl-gitops); nothing here deploys or configures it.

Usage (offline, read-only; inputs are files):
  temporal workflow show -w ID -o json > history.json
  python tools/temporal_nondeterminism_scan.py history history.json [more.json ...]
  curl -s http://FRONTEND_OR_HISTORY_METRICS_ENDPOINT/metrics > scrape.txt
  python tools/temporal_nondeterminism_scan.py metrics scrape.txt

Exit codes:
  0  every input is CLEAN
  2  BLOCKING: at least one input is UNKNOWN (outranks 3)
  3  at least one input records NONDETERMINISM and none is UNKNOWN

Detector acceptance (AGENTS.md): tests/test_temporal_nondeterminism_scan.py
proves it green on converged input and red under deliberate mutations of this
file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ERROR_CODE = "TMPRL1100"
MESSAGE_PREFIX = f"[{ERROR_CODE}] Nondeterminism error"
CAUSE = "WORKFLOW_TASK_FAILED_CAUSE_NON_DETERMINISTIC_ERROR"
FAILED_TASK_EVENT = "EVENT_TYPE_WORKFLOW_TASK_FAILED"
STARTED_EVENT = "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED"
#: Server counter (Temporal Server 1.32.0 dev server, observed 2026-11-08).
METRIC = "failed_workflow_tasks"
METRIC_LABEL = "failure"
METRIC_VALUE = "NonDeterministicError"
#: A series every healthy scrape carries, so an empty or truncated scrape is
#: UNKNOWN rather than "no failures". Counters of this server appear only after
#: their first increment, so the missing failure series alone proves nothing.
LIVENESS_METRIC = "workflow_tasks_completed"

NONDETERMINISM, CLEAN, UNKNOWN = "NONDETERMINISM", "CLEAN", "UNKNOWN"
EXIT_OK, EXIT_BLOCKING, EXIT_DETECTED = 0, 2, 3


@dataclass(frozen=True)
class Verdict:
    verdict: str
    detail: str


def _is_nondeterminism(attrs: Any) -> bool:
    if not isinstance(attrs, dict):
        return False
    failure = attrs.get("failure")
    message = failure.get("message") if isinstance(failure, dict) else None
    by_message = isinstance(message, str) and message.startswith(MESSAGE_PREFIX)
    return attrs.get("cause") == CAUSE or by_message


def classify_history(raw: str | bytes | None) -> Verdict:
    """Classify one serialized history (proto JSON, `{"events": [...]}`)."""
    if raw is None:
        return Verdict(UNKNOWN, "no history")
    try:
        doc = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - a corrupt or truncated body is UNKNOWN
        return Verdict(UNKNOWN, f"unparseable: {type(exc).__name__}")
    events = doc.get("events") if isinstance(doc, dict) else doc
    if not isinstance(events, list) or not events or not all(isinstance(e, dict) for e in events):
        return Verdict(UNKNOWN, "no events")
    try:
        ids = [int(e["eventId"]) for e in events]
    except Exception:  # noqa: BLE001
        return Verdict(UNKNOWN, "event without eventId")
    if ids != list(range(1, len(ids) + 1)) or events[0].get("eventType") != STARTED_EVENT:
        return Verdict(UNKNOWN, "history is not contiguous from the start event")
    hits = [
        e["eventId"]
        for e in events
        if e.get("eventType") == FAILED_TASK_EVENT and _is_nondeterminism(e.get("workflowTaskFailedEventAttributes"))
    ]
    if hits:
        return Verdict(NONDETERMINISM, f"workflow task failed with {ERROR_CODE} at event {hits[0]}")
    return Verdict(CLEAN, "no non-determinism task failure recorded so far")


_SAMPLE = re.compile(r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)")
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


def classify_metrics(text: str | bytes | None) -> Verdict:
    """Classify one Prometheus text scrape of a Temporal server."""
    if text is None:
        return Verdict(UNKNOWN, "no scrape")
    if isinstance(text, bytes):
        try:
            text = text.decode()
        except UnicodeDecodeError:
            return Verdict(UNKNOWN, "scrape is not text")
    alive = False
    total = 0.0
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE.match(line)
        if not m:
            return Verdict(UNKNOWN, "unparseable sample line")
        try:
            value = float(m["value"])
        except ValueError:
            return Verdict(UNKNOWN, "non-numeric sample value")
        if m["name"] == LIVENESS_METRIC:
            alive = True
        if m["name"] == METRIC and dict(_LABEL.findall(m["labels"] or "")).get(METRIC_LABEL) == METRIC_VALUE:
            total += value
    if total > 0:
        return Verdict(NONDETERMINISM, f"{METRIC}{{{METRIC_LABEL}={METRIC_VALUE!r}}} = {total:g}")
    if not alive:
        return Verdict(UNKNOWN, f"scrape has no {LIVENESS_METRIC} series, so zero failures proves nothing")
    return Verdict(CLEAN, "no non-determinism task failure counted")


def exit_code(verdicts: list[Verdict]) -> int:
    """UNKNOWN outranks a detection; an empty list is UNKNOWN, not clean."""
    if not verdicts or any(v.verdict == UNKNOWN for v in verdicts):
        return EXIT_BLOCKING
    if any(v.verdict == NONDETERMINISM for v in verdicts):
        return EXIT_DETECTED
    return EXIT_OK


def _read(path: str) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Detect Temporal non-determinism task failures in recorded evidence.")
    p.add_argument("kind", choices=("history", "metrics"))
    p.add_argument("files", nargs="+")
    args = p.parse_args(argv)
    classify = classify_history if args.kind == "history" else classify_metrics
    verdicts = []
    for name in args.files:
        v = classify(_read(name))
        verdicts.append(v)
        print(f"{v.verdict}\t{name}\t{v.detail}")
    code = exit_code(verdicts)
    print(f"exit={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
