"""Decide whether a proposal's source issue is a spike (mctl-agents#763).

A merged research PR must not auto-close a spike issue: the owner records
``decision.verdict`` in the SpikeDefinition manifest first and closes the
issue by hand. So the implementer asks, immediately before opening a PR,
whether the source issue is a spike, and emits ``Refs`` instead of ``Closes``
unless it can positively establish that it is not.

Fail-safe: every failure to find out is an ``observation-error`` and the
caller treats it like a spike. Only ``not-spike`` permits ``Closes``.

Depends on nothing from ``run_implementer`` / ``run_shepherd`` (import
cycles); the GitHub reader is caller-supplied, like ``source_issue``.
"""
from __future__ import annotations

import base64
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import yaml

GhApiJson = Callable[[list[str]], Any]

SPIKE_TITLE_RE = re.compile(r"^\s*spike\s*[(:]", re.IGNORECASE)
SPIKES_REPO = "mctlhq/.github"
SPIKES_DIR = "roadmap/spikes"

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")

SPIKE_TITLE = "spike-title"
SPIKE_MANIFEST = "spike-manifest"
NOT_SPIKE = "not-spike"
OBSERVATION_ERROR = "observation-error"


@dataclass(frozen=True)
class SpikeClassification:
    outcome: str
    detail: str | None = None

    @property
    def is_spike_or_unknown(self) -> bool:
        return self.outcome != NOT_SPIKE


def _parse_issue_binding(value: Any) -> tuple[str, int]:
    """Parse ``spec.github.issue`` -- the schema's ``issueRef`` shape only.

    ``{repository: "<owner>/<repo>", number: <int >= 1>}`` with no other
    keys. Returns ``(repository_lower, number)`` or raises ``ValueError``.
    """
    if not isinstance(value, dict) or set(value) != {"repository", "number"}:
        raise ValueError("spec.github.issue is not a {repository, number} mapping")
    repository = value["repository"]
    number = value["number"]
    if not isinstance(repository, str) or not _REPOSITORY_RE.match(repository):
        raise ValueError("spec.github.issue.repository is not owner/repo")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError("spec.github.issue.number is not an integer >= 1")
    return repository.lower(), number


def _error(step: str, exc: BaseException | str) -> SpikeClassification:
    return SpikeClassification(OBSERVATION_ERROR, f"step={step} error={exc}")


def classify_issue(
    repo: str, number: int, *, gh_api_json: GhApiJson
) -> SpikeClassification:
    try:
        issue = gh_api_json([f"repos/{repo}/issues/{number}"])
        title = issue.get("title") if isinstance(issue, dict) else None
        if not isinstance(title, str):
            raise ValueError("issue payload has no string title")
    except Exception as exc:  # noqa: BLE001 - fail safe on any read failure
        return _error("issue-read", exc)

    if SPIKE_TITLE_RE.match(title):
        return SpikeClassification(SPIKE_TITLE)

    try:
        listing = gh_api_json([f"repos/{SPIKES_REPO}/contents/{SPIKES_DIR}"])
        if not isinstance(listing, list):
            raise ValueError("spike listing is not a list")
    except Exception as exc:  # noqa: BLE001
        return _error("spike-list", exc)

    want = (repo.lower(), int(number))
    matched = False
    for entry in listing:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name.endswith((".yaml", ".yml")):
            continue
        step = f"spike-manifest:{name}"
        try:
            payload = gh_api_json([f"repos/{SPIKES_REPO}/contents/{SPIKES_DIR}/{name}"])
            if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
                raise ValueError("manifest payload has no content")
            text = base64.b64decode(payload["content"]).decode("utf-8")
            doc = yaml.safe_load(text)
            spec = doc.get("spec") if isinstance(doc, dict) else None
            gh = spec.get("github") if isinstance(spec, dict) else None
            if not isinstance(gh, dict) or "issue" not in gh:
                raise ValueError("spec.github.issue is missing")
            binding = _parse_issue_binding(gh["issue"])
        except Exception as exc:  # noqa: BLE001
            return _error(step, exc)
        if binding == want:
            matched = True
    if matched:
        return SpikeClassification(SPIKE_MANIFEST)
    return SpikeClassification(NOT_SPIKE)
