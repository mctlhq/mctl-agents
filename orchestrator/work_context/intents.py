"""mctl-api's canonical WorkItem intents (mctlhq/mctl-api#430), as a
ContextSnapshot source (mctlhq/mctl-agents#542, correction 2026-09-30).

An intent is provenance and input: a bounded record of what was asked for.
It is never authorization or approval state, and nothing in this module
decides anything with it.

Every classifier keeps "could not observe" apart from "observed absent"
(AGENTS.md, "Detectors, Reconcilers and Review Gates"):

- a transport error, a 5xx, a malformed body, a page that does not describe
  the item asked about, or a truncated page whose continuation cannot be
  read is UNKNOWN;
- only mctl-api's documented absence signals are ABSENT: a 404 with
  `intent_not_found` for one intent, and a 404 with `work_item_not_found`
  for the item;
- a 200 listing whose `intents` list is empty is neither: it is LISTED with
  no intents. The item exists and was read completely; it simply has no
  intents yet. That is a successful observation the run proceeds on (and
  records its high-water mark from), whereas ABSENT and UNKNOWN both mean
  the intents could not be established and fail the run as unresolved.

A retention-swept intent reads back with `text: ""` and
`text_redacted: true`. That is "the text is gone", never "the text was
empty", so `text_redacted` is carried on the mirror and into provenance.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "workitem/v1"

#: `WORK_ITEM_INTENT_SOURCE` (`off` | `on`, default `off`). Read per call.
SWITCH_ENV_VAR = "WORK_ITEM_INTENT_SOURCE"
SWITCH_OFF = "off"
SWITCH_ON = "on"

INTENTS_LISTED = "intents-listed"
INTENT_FOUND = "intent-found"
INTENT_ABSENT = "intent-absent"
INTENTS_UNKNOWN = "intents-unknown"

INTENT_NOT_FOUND_CODE = "intent_not_found"
WORK_ITEM_NOT_FOUND_CODE = "work_item_not_found"

#: mctl-api's page ceiling (workitems.MaxIntentPageLimit).
PAGE_LIMIT = 100
#: A bound on how many pages one listing reads. A longer listing is
#: UNKNOWN, never silently cut short.
MAX_PAGES = 50


def switch() -> str:
    """`on` only for exactly `on`. Unset or `off` is `off`; anything else is
    `off` with a warning, so an unrecognised value never enables the source."""
    raw = os.environ.get(SWITCH_ENV_VAR, SWITCH_OFF)
    value = raw.strip().lower()
    if value in (SWITCH_OFF, ""):
        return SWITCH_OFF
    if value == SWITCH_ON:
        return SWITCH_ON
    print(f"warn: {SWITCH_ENV_VAR}={raw!r} is not one of 'off', 'on'; treating it as 'off'")
    return SWITCH_OFF


@dataclass(frozen=True)
class Intent:
    """mctl-api's `workitems.Intent`, exactly the fields it serializes."""

    intent_id: int
    work_item_id: str
    actor_principal: str
    surface: str
    text: str
    text_redacted: bool
    params: Any
    created_at: str

    @staticmethod
    def from_payload(data: Any) -> Intent | None:
        """None for anything that is not a complete intent: a missing or
        mistyped field is a malformed body, not a default."""
        if not isinstance(data, dict):
            return None
        iid = data.get("id")
        if not isinstance(iid, int) or isinstance(iid, bool) or iid <= 0:
            return None
        wid, actor, text, created = (
            data.get("work_item_id"), data.get("actor_principal"), data.get("text"), data.get("created_at"),
        )
        if not isinstance(wid, str) or not wid or not isinstance(actor, str) or not actor:
            return None
        if not isinstance(text, str) or not isinstance(created, str) or not created:
            return None
        redacted = data.get("text_redacted")
        # Always serialized by mctl-api (never omitted), so its absence is a
        # malformed body, never an implicit `false`.
        if not isinstance(redacted, bool):
            return None
        surface = data.get("surface", "")
        if not isinstance(surface, str):
            return None
        return Intent(
            intent_id=iid,
            work_item_id=wid,
            actor_principal=actor,
            surface=surface,
            text=text,
            text_redacted=redacted,
            params=data.get("params"),
            created_at=created,
        )


@dataclass(frozen=True)
class IntentPage:
    """One page of a listing, already validated."""

    intents: tuple[Intent, ...]
    truncated: bool


@dataclass(frozen=True)
class IntentsAnswer:
    """The answer to a listing (`intents`) or a single read (`intent`)."""

    verdict: str
    intents: tuple[Intent, ...] = ()
    intent: Intent | None = None
    reason: str = ""


def _code(payload: dict[str, Any]) -> str:
    code = payload.get("code")
    return code if isinstance(code, str) else ""


def page_from(
    status: int, payload: dict[str, Any], *, work_item_id: str, after_id: int
) -> tuple[IntentPage | None, IntentsAnswer | None]:
    """One listing page. Returns the page, or the terminal answer when the
    page cannot be used: ABSENT for the documented item 404, UNKNOWN for
    anything else. A page is used only when every intent parses, belongs to
    `work_item_id`, and ids ascend strictly past `after_id`."""
    if status == 404 and _code(payload) == WORK_ITEM_NOT_FOUND_CODE:
        return None, IntentsAnswer(INTENT_ABSENT, reason=f"work item {work_item_id} not found")
    if status != 200:
        return None, IntentsAnswer(INTENTS_UNKNOWN, reason=f"HTTP {status} {_code(payload)}".strip())
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None, IntentsAnswer(INTENTS_UNKNOWN, reason="listing is not a workitem/v1 answer")
    raw, truncated = payload.get("intents"), payload.get("truncated")
    if not isinstance(raw, list) or not isinstance(truncated, bool):
        return None, IntentsAnswer(INTENTS_UNKNOWN, reason="listing has no intents list or truncated flag")
    out: list[Intent] = []
    last = after_id
    for item in raw:
        intent = Intent.from_payload(item)
        if intent is None:
            return None, IntentsAnswer(INTENTS_UNKNOWN, reason="listing carries a malformed intent")
        if intent.work_item_id != work_item_id:
            return None, IntentsAnswer(
                INTENTS_UNKNOWN, reason=f"listing for {work_item_id} carries an intent of {intent.work_item_id}"
            )
        if intent.intent_id <= last:
            return None, IntentsAnswer(INTENTS_UNKNOWN, reason="listing is not in strictly ascending id order")
        last = intent.intent_id
        out.append(intent)
    if truncated and not out:
        # A truncated page with nothing on it gives no id to continue from.
        return None, IntentsAnswer(INTENTS_UNKNOWN, reason="truncated listing page with no intents")
    return IntentPage(intents=tuple(out), truncated=truncated), None


def answer_from_read(status: int, payload: dict[str, Any], *, work_item_id: str, intent_id: int) -> IntentsAnswer:
    """One intent. FOUND only for exactly that intent of exactly that item;
    ABSENT for the documented 404s; UNKNOWN otherwise."""
    if status == 404 and _code(payload) in (INTENT_NOT_FOUND_CODE, WORK_ITEM_NOT_FOUND_CODE):
        return IntentsAnswer(INTENT_ABSENT, reason=f"HTTP 404 {_code(payload)}")
    if status != 200:
        return IntentsAnswer(INTENTS_UNKNOWN, reason=f"HTTP {status} {_code(payload)}".strip())
    if payload.get("schema_version") != SCHEMA_VERSION:
        return IntentsAnswer(INTENTS_UNKNOWN, reason="read is not a workitem/v1 answer")
    intent = Intent.from_payload(payload.get("intent"))
    if intent is None:
        return IntentsAnswer(INTENTS_UNKNOWN, reason="read carries a malformed intent")
    if intent.intent_id != intent_id or intent.work_item_id != work_item_id:
        return IntentsAnswer(
            INTENTS_UNKNOWN,
            reason=f"asked for intent {intent_id} of {work_item_id}, the store answered "
            f"{intent.intent_id} of {intent.work_item_id}",
        )
    return IntentsAnswer(INTENT_FOUND, intent=intent)
