"""Tests for tools/temporal_nondeterminism_scan.py (mctl-agents#1007).

A detector is accepted only when it is green on converged input AND red on a
deliberate mutation (AGENTS.md). Three layers:

1. `detector_checks(mod)`: every assertion that defines a correct detector,
   written against a module object so it can be pointed at any variant.
2. Plain tests: the real tool passes `detector_checks`, plus the CLI.
3. Mutation tests: each rewrites one snippet of the tool source into a temp
   module and requires `detector_checks` to FAIL against it. Each asserts its
   snippet still exists, so a refactor cannot turn a mutation into a no-op.

Evidence behind the fixtures. `nondeterminism_marker_rollback.json` is a REAL
history recorded on a local dev server (Temporal Server 1.32.0, CLI 1.9.1,
Python SDK 1.34.0): a workflow ran under a worker that records the patch
marker `p-x`, then a worker without that patch handled the next signal. The
only edit is that worker identities were replaced by `worker-redacted`. The
metrics sample lines are copied from the same server's scrape.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parent.parent
_TOOL = _REPO / "tools" / "temporal_nondeterminism_scan.py"
_FIX = _REPO / "tests" / "fixtures" / "histories"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


nd = _load(_TOOL, "temporal_nondeterminism_scan")
POSITIVE = (_FIX / "nondeterminism_marker_rollback.json").read_text()
# Converged inputs: recorded histories in which no workflow task failed.
CLEAN_FIXTURES = ["dev_loop_deferred_resume.json", "dev_loop_parked.json", "incident_loop.patched.json"]

_LIVE = (
    'workflow_tasks_completed{first_attempt="true",namespace="default",operation="WorkflowContext",'
    'service_name="history",versioning_behavior="Unspecified"} 1\n'
)
_FAIL = (
    'failed_workflow_tasks{failure="%s",first_attempt="true",namespace="default",'
    'operation="RespondWorkflowTaskFailed",service_name="history",versioning_behavior="Unspecified"} %s\n'
)


def _mutate_history(mutate) -> str:
    doc = json.loads(POSITIVE)
    mutate(doc["events"])
    return json.dumps(doc)


def _failed_attrs(events: list) -> dict:
    return next(e for e in events if e["eventType"].endswith("WORKFLOW_TASK_FAILED"))[
        "workflowTaskFailedEventAttributes"
    ]


def detector_checks(mod: ModuleType) -> None:
    """Everything that defines a correct detector, against any module variant."""
    # Positive control: the real recorded failure is detected.
    v = mod.classify_history(POSITIVE)
    assert v.verdict == mod.NONDETERMINISM and "event 10" in v.detail
    # Converged inputs are clean.
    for name in CLEAN_FIXTURES:
        assert mod.classify_history((_FIX / name).read_text()).verdict == mod.CLEAN, name
    # Cause alone is enough (message wording may change between SDK versions).
    assert (
        mod.classify_history(_mutate_history(lambda ev: _failed_attrs(ev).pop("failure"))).verdict == mod.NONDETERMINISM
    )
    # The message alone is enough (cause enum may change).
    assert (
        mod.classify_history(
            _mutate_history(lambda ev: _failed_attrs(ev).update(cause="WORKFLOW_TASK_FAILED_CAUSE_UNSPECIFIED"))
        ).verdict
        == mod.NONDETERMINISM
    )

    # A different task failure is not non-determinism.
    def other_failure(ev: list) -> None:
        a = _failed_attrs(ev)
        a["cause"] = "WORKFLOW_TASK_FAILED_CAUSE_WORKFLOW_WORKER_UNHANDLED_FAILURE"
        a["failure"]["message"] = "ValueError: boom"

    assert mod.classify_history(_mutate_history(other_failure)).verdict == mod.CLEAN
    # The failure event type matters: the same attributes on another event are not a task failure.
    assert (
        mod.classify_history(
            _mutate_history(
                lambda ev: next(e for e in ev if e["eventId"] == "10").update(
                    eventType="EVENT_TYPE_WORKFLOW_TASK_COMPLETED"
                )
            )
        ).verdict
        == mod.CLEAN
    )
    # Could not observe is UNKNOWN, never CLEAN.
    for raw in (
        None,
        "",
        "{",
        "not json",
        "[]",
        '{"events": []}',
        '{"events": [1]}',
        '{"events": [{"eventType": "x"}]}',
    ):
        assert mod.classify_history(raw).verdict == mod.UNKNOWN, raw
    # A history cut before the failure is not "clean": a gap in event ids is UNKNOWN.
    assert mod.classify_history(_mutate_history(lambda ev: ev.pop(3))).verdict == mod.UNKNOWN
    # A history that does not start at the start event is UNKNOWN.
    assert (
        mod.classify_history(_mutate_history(lambda ev: ev[0].update(eventType="EVENT_TYPE_MARKER_RECORDED"))).verdict
        == mod.UNKNOWN
    )

    # Metrics: failure counted -> detected; live scrape with no failure series -> clean.
    assert mod.classify_metrics(_LIVE + _FAIL % ("NonDeterministicError", "2")).verdict == mod.NONDETERMINISM
    assert mod.classify_metrics(_LIVE).verdict == mod.CLEAN
    assert mod.classify_metrics(_LIVE + _FAIL % ("NonDeterministicError", "0")).verdict == mod.CLEAN
    # Other failure kinds do not count; two series add up.
    assert mod.classify_metrics(_LIVE + _FAIL % ("UnhandledCommand", "5")).verdict == mod.CLEAN
    two = (
        _LIVE + _FAIL % ("NonDeterministicError", "0") + _FAIL.replace("history", "x") % ("NonDeterministicError", "1")
    )
    assert mod.classify_metrics(two).verdict == mod.NONDETERMINISM
    two_rev = (
        _LIVE + _FAIL % ("NonDeterministicError", "1") + _FAIL.replace("history", "x") % ("NonDeterministicError", "0")
    )
    assert mod.classify_metrics(two_rev).verdict == mod.NONDETERMINISM
    # Could not observe is UNKNOWN: empty, missing, truncated or foreign scrape, bad lines.
    for text in (
        None,
        "",
        "# TYPE x counter\n",
        "some_other_metric 1\n",
        b"\xff\xfe",
        _LIVE + "!garbage\n",
        _LIVE + "failed_workflow_tasks 1x\n",
    ):
        assert mod.classify_metrics(text).verdict == mod.UNKNOWN, text
    # Detection wins over a missing liveness series only when a failure is actually counted.
    assert mod.classify_metrics(_FAIL % ("NonDeterministicError", "1")).verdict == mod.NONDETERMINISM

    # Exit codes: UNKNOWN outranks a detection, empty is blocking, clean is the only 0.
    V = mod.Verdict
    assert mod.exit_code([V(mod.CLEAN, "")]) == 0
    assert mod.exit_code([V(mod.CLEAN, ""), V(mod.NONDETERMINISM, "")]) == 3
    assert mod.exit_code([V(mod.NONDETERMINISM, ""), V(mod.UNKNOWN, "")]) == 2
    assert mod.exit_code([V(mod.CLEAN, ""), V(mod.UNKNOWN, "")]) == 2
    assert mod.exit_code([]) == 2


def test_real_tool_passes_detector_checks():
    detector_checks(nd)


def test_recorded_history_is_a_real_failure_not_a_synthetic_shape():
    events = json.loads(POSITIVE)["events"]
    failed = [e for e in events if e["eventType"] == nd.FAILED_TASK_EVENT]
    assert len(failed) == 1
    attrs = failed[0]["workflowTaskFailedEventAttributes"]
    assert attrs["cause"] == nd.CAUSE and attrs["failure"]["message"].startswith(nd.MESSAGE_PREFIX)
    assert "worker-redacted" in POSITIVE and "@" not in POSITIVE.replace("worker-redacted", "")


def test_cli_exit_codes_and_unreadable_file(tmp_path, capsys):
    clean = _FIX / "dev_loop_deferred_resume.json"
    pos = _FIX / "nondeterminism_marker_rollback.json"
    assert nd.main(["history", str(clean)]) == 0
    assert nd.main(["history", str(clean), str(pos)]) == 3
    # A file that cannot be read is blocking, not absent.
    assert nd.main(["history", str(clean), str(tmp_path / "missing.json")]) == 2
    scrape = tmp_path / "scrape.txt"
    scrape.write_text(_LIVE)
    assert nd.main(["metrics", str(scrape)]) == 0
    scrape.write_text(_LIVE + _FAIL % ("NonDeterministicError", "1"))
    assert nd.main(["metrics", str(scrape)]) == 3
    assert "NONDETERMINISM" in capsys.readouterr().out


# (snippet, replacement). The snippet must occur in the tool source.
MUTATIONS: dict[str, tuple[str, str]] = {
    "cause-ignored": ('return attrs.get("cause") == CAUSE or by_message', "return by_message"),
    "message-ignored": ('return attrs.get("cause") == CAUSE or by_message', 'return attrs.get("cause") == CAUSE'),
    "any-task-failure-counts": ('return attrs.get("cause") == CAUSE or by_message', "return True"),
    "event-type-not-checked": ('if e.get("eventType") == FAILED_TASK_EVENT and ', "if "),
    "no-history-becomes-clean": ('return Verdict(UNKNOWN, "no history")', 'return Verdict(CLEAN, "no history")'),
    "unparseable-becomes-clean": ('return Verdict(UNKNOWN, f"unparseable:', 'return Verdict(CLEAN, f"unparseable:'),
    "empty-events-becomes-clean": ('return Verdict(UNKNOWN, "no events")', 'return Verdict(CLEAN, "no events")'),
    "event-without-id-becomes-clean": (
        'return Verdict(UNKNOWN, "event without eventId")',
        'return Verdict(CLEAN, "event without eventId")',
    ),
    "id-gap-ignored": ("if ids != list(range(1, len(ids) + 1)) or events[0]", "if events[0]"),
    "start-event-not-checked": (' or events[0].get("eventType") != STARTED_EVENT', ""),
    "scrape-missing-becomes-clean": ('return Verdict(UNKNOWN, "no scrape")', 'return Verdict(CLEAN, "no scrape")'),
    "bad-sample-ignored": ('return Verdict(UNKNOWN, "unparseable sample line")', "continue"),
    "bad-value-ignored": ('return Verdict(UNKNOWN, "non-numeric sample value")', "continue"),
    "liveness-not-required": ("if not alive:", "if False:"),
    "metric-label-not-compared": ('dict(_LABEL.findall(m["labels"] or "")).get(METRIC_LABEL) == METRIC_VALUE', "True"),
    "zero-counts-as-failure": ("if total > 0:", "if total >= 0:"),
    "series-not-summed": ("total += value", "total = value"),
    "undecodable-scrape-becomes-clean": (
        'return Verdict(UNKNOWN, "scrape is not text")',
        'text = "workflow_tasks_completed 1\\n"',
    ),
    "unknown-does-not-block": ("if not verdicts or any(v.verdict == UNKNOWN for v in verdicts):", "if not verdicts:"),
    "empty-input-is-ok": ("if not verdicts or any(", "if any("),
    "detection-does-not-exit": ("if any(v.verdict == NONDETERMINISM for v in verdicts):", "if False:"),
    "detection-outranks-unknown": (
        "if not verdicts or any(v.verdict == UNKNOWN for v in verdicts):\n        return EXIT_BLOCKING\n"
        "    if any(v.verdict == NONDETERMINISM for v in verdicts):\n        return EXIT_DETECTED",
        "if any(v.verdict == NONDETERMINISM for v in verdicts):\n        return EXIT_DETECTED\n"
        "    if not verdicts or any(v.verdict == UNKNOWN for v in verdicts):\n        return EXIT_BLOCKING",
    ),
    "cli-drops-unreadable-input": (
        "        verdicts.append(v)\n",
        "        if v.verdict != UNKNOWN:\n            verdicts.append(v)\n",
    ),
}


def _mutant(tmp_path: Path, name: str) -> ModuleType:
    old, new = MUTATIONS[name]
    src = _TOOL.read_text()
    assert old in src, f"mutation {name!r}: snippet vanished from the tool; update the mutation"
    path = tmp_path / f"mutant_{name.replace('-', '_')}.py"
    path.write_text(src.replace(old, new))
    return _load(path, f"mutant_{name.replace('-', '_')}")


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_mutation_is_killed(tmp_path, name):
    mutant = _mutant(tmp_path, name)
    try:
        detector_checks(mutant)
        if name.startswith("cli-"):  # lives in the CLI, not in the verdicts
            clean = str(_FIX / "dev_loop_deferred_resume.json")
            assert mutant.main(["history", clean, str(tmp_path / "missing.json")]) == 2
    except AssertionError:
        return  # killed by a detector assertion
    except Exception as exc:  # noqa: BLE001 - a crash is not a kill
        pytest.fail(f"mutant {name!r} crashed with {type(exc).__name__}: {exc}; that is not a detector kill")
    pytest.fail(f"mutant {name!r} survived the detector assertions")


def test_mutation_harness_is_green_on_an_unmutated_copy(tmp_path):
    path = tmp_path / "copy_of_tool.py"
    path.write_text(_TOOL.read_text())
    detector_checks(_load(path, "copy_of_tool"))
