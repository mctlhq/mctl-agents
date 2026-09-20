"""Unit tests for orchestrator.directives (mctl-agents#417).

Table-driven over parse_comment/parse_comments — the pure recognition rule
that carries the whole security posture (see the module docstring). No
`gh`, no Temporal, no filesystem.
"""
from __future__ import annotations

import dataclasses

import pytest

from orchestrator.directives import (
    BOT_LOGINS,
    PRIVILEGED_ASSOCIATIONS,
    VERBS,
    Directive,
    RawComment,
    ack_trailer,
    acked_comment_ids,
    fail_trailer,
    failed_attempt_counts,
    parse_comment,
    parse_comments,
)

_BOT_LOGIN = next(iter(BOT_LOGINS))


def _comment(
    body: str,
    *,
    id: str = "c1",
    author: str = "octocat",
    created_at: str = "2026-09-19T12:00:00Z",
    author_association: str = "OWNER",
) -> RawComment:
    return RawComment(
        id=id, author=author, created_at=created_at, body=body,
        author_association=author_association,
    )


# ---------------------------------------------------------------------------
# parse_comment — table-driven
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body,expected_verb",
    [
        ("@MCTL reinvestigate", "reinvestigate"),
        ("@mctl reinvestigate", "reinvestigate"),           # lowercase mention
        ("@MCTL Reinvestigate", "reinvestigate"),            # mixed-case verb
        ("@MCTL REINVESTIGATE\nplease", "reinvestigate"),
        ("@mctl-agents reinvestigate", "reinvestigate"),
        ("@mctl-agents[bot] reinvestigate", "reinvestigate"),
    ],
)
def test_recognised_verb(body, expected_verb):
    directive = parse_comment(_comment(body))
    assert directive is not None
    assert directive.verb == expected_verb
    assert directive.comment_id == "c1"


def test_unrecognised_verb_after_a_mention_is_not_none():
    """A mention with no supported verb is `unrecognised`, not silence —
    the whole point of #417."""
    directive = parse_comment(_comment("@MCTL please cancel this"))
    assert directive is not None
    assert directive.verb is None


def test_mention_with_nothing_after_is_unrecognised():
    directive = parse_comment(_comment("@MCTL"))
    assert directive is not None
    assert directive.verb is None


def test_no_mention_is_not_a_directive():
    assert parse_comment(_comment("please look at this again")) is None


def test_bot_authored_comment_is_not_a_directive():
    """Otherwise the ack reply this module's caller posts would be re-read
    as a new request on the next tick."""
    for login in BOT_LOGINS:
        directive = parse_comment(_comment("@MCTL reinvestigate", author=login))
        assert directive is None, login


def test_bot_author_check_is_case_insensitive():
    directive = parse_comment(_comment("@MCTL reinvestigate", author="MCTL-AGENTS[BOT]"))
    assert directive is None


def test_mention_mid_line_does_not_count():
    """Only a LINE-INITIAL mention is a directive."""
    directive = parse_comment(_comment("please @MCTL reinvestigate this"))
    assert directive is None


def test_a_quoted_mention_does_not_count():
    """A markdown blockquote line begins with '>', not with the mention
    token, so it naturally fails the line-initial check."""
    directive = parse_comment(_comment("> @MCTL reinvestigate"))
    assert directive is None


def test_a_login_failing_the_login_pattern_is_refused():
    directive = parse_comment(_comment("@MCTL reinvestigate", author="not a login!"))
    assert directive is None


@pytest.mark.parametrize("association", sorted(PRIVILEGED_ASSOCIATIONS))
def test_privileged_associations_are_authorized(association):
    directive = parse_comment(_comment("@MCTL reinvestigate", author_association=association))
    assert directive is not None
    assert directive.authorized is True


@pytest.mark.parametrize("association", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", ""])
def test_unprivileged_associations_are_not_authorized(association):
    directive = parse_comment(_comment("@MCTL reinvestigate", author_association=association))
    assert directive is not None
    assert directive.authorized is False


def test_verbs_vocabulary_is_exactly_reinvestigate():
    assert VERBS == frozenset({"reinvestigate"})


def test_parse_comments_drops_non_directives():
    comments = [
        _comment("just a regular comment", id="a"),
        _comment("@MCTL reinvestigate", id="b"),
        _comment("@MCTL reinvestigate", id="c", author=next(iter(BOT_LOGINS))),
    ]
    directives = parse_comments(comments)
    assert [d.comment_id for d in directives] == ["b"]


# ---------------------------------------------------------------------------
# ack_trailer / acked_comment_ids
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "comment_id",
    ["IC_kwDOAbC1Y87abc123", "with-dash", "with_underscore", "mixed-id_123"],
)
def test_ack_trailer_round_trips(comment_id):
    trailer = ack_trailer(comment_id)
    ids = acked_comment_ids([_comment(f"reply text\n\n{trailer}", author=_BOT_LOGIN)])
    assert ids == {comment_id}


def test_a_quoted_trailer_inside_a_fenced_code_block_still_counts_as_acked():
    """The conservative direction: a false 'already acked' costs one
    missed retry; a false 'not acked' costs a duplicate SDK run."""
    body = "some text\n```\n" + ack_trailer("c42") + "\n```\nmore text"
    assert acked_comment_ids([_comment(body, author=_BOT_LOGIN)]) == {"c42"}


def test_a_comment_carrying_two_trailers_yields_both_ids():
    body = ack_trailer("c1") + "\n" + ack_trailer("c2")
    assert acked_comment_ids([_comment(body, author=_BOT_LOGIN)]) == {"c1", "c2"}


def test_acked_comment_ids_ignores_a_trailer_from_a_non_bot_author():
    """mctl-agents#417 codex review: honouring an ack trailer from ANY
    author would let an unprivileged commenter forge one and silently
    suppress a maintainer's directive. Only the bot's own comments count."""
    comments = [
        _comment(f"whatever\n\n{ack_trailer('x1')}", id="1", author="someone"),
        _comment(f"whatever\n\n{ack_trailer('x2')}", id="2", author=_BOT_LOGIN),
    ]
    assert acked_comment_ids(comments) == {"x2"}


def test_no_ack_trailer_yields_empty_set():
    assert acked_comment_ids([_comment("no trailer here", author=_BOT_LOGIN)]) == set()


# ---------------------------------------------------------------------------
# fail_trailer / failed_attempt_counts
# ---------------------------------------------------------------------------
def test_fail_trailer_round_trips():
    trailer = fail_trailer("c1")
    assert failed_attempt_counts([_comment(f"reply\n\n{trailer}")]) == {"c1": 1}


def test_failed_attempt_counts_accumulates_across_comments():
    comments = [
        _comment(f"attempt 1\n\n{fail_trailer('c1')}", id="a"),
        _comment(f"attempt 2\n\n{fail_trailer('c1')}", id="b"),
        _comment(f"different directive\n\n{fail_trailer('c2')}", id="c"),
    ]
    assert failed_attempt_counts(comments) == {"c1": 2, "c2": 1}


def test_no_fail_trailer_yields_empty_dict():
    assert failed_attempt_counts([_comment("no trailer here")]) == {}


def test_directive_is_a_frozen_dataclass():
    d = Directive(comment_id="1", author="a", created_at="t", verb=None, authorized=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.verb = "reinvestigate"  # type: ignore[misc]
