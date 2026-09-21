"""Choose the one proposal directory that owns an issue number.

Two independent paths answer "which proposal dir belongs to issue <N>":
``run_issue_investigator.resolve_slug`` (local filesystem, before writing)
and ``temporal.activities.proposals.find_proposal_slug`` (GitHub contents
API, before implementing). Both key on the ``issue-<N>-`` prefix, because
the number is the part of a slug that cannot change while the title can
(#246). Both used to refuse outright as soon as two directories matched.

That refusal is right for two LIVE proposals — picking one would implement
work nobody chose. It is wrong for the case that actually occurs in
production: a proposal whose implementation PR was closed unmerged is
rewritten to ``rejected`` and can never be acted on again (mctl-agents#438),
yet it kept blocking every later lookup for its issue, so an issue could
not be given a replacement proposal at all.

So the rule is narrow on purpose:

- one directory — return it, whatever its status (unchanged behaviour);
- several — drop the ``rejected`` ones, and return the single survivor;
- anything else — refuse, naming every candidate.

Deliberately NOT in this rule, because each would turn "refuse to guess"
back into guessing:

- ``merged`` is NOT ignored. A ``merged`` proposal beside an ``accepted``
  one is a real question about what a reopened or continued issue means,
  and it stays an ambiguity until that is decided on its own terms.
- No newest-wins tie-break on ``updated_at``. Timestamps are written by
  several actors and a stale clock would silently pick the wrong work.
- No lexical tie-break on the slug itself. A ``-v2`` suffix sorting last is
  a coincidence of naming, not a statement about which proposal is real.

A status this module cannot read counts as LIVE, never as ignorable: an
unreadable or absent ``.status.yaml`` is missing evidence, and missing
evidence must not retire a candidate.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: The only status a candidate may be retired for. See the module docstring
#: for why this list is one item long and should stay hard to grow.
IGNORABLE_STATUSES = frozenset({"rejected"})


class AmbiguousProposalError(Exception):
    """Several live proposal directories claim one issue number.

    Callers translate this into whatever their layer raises — a
    non-retryable ``ApplicationError`` inside an activity, a
    ``ProposalAmbiguityError`` in the investigator — so the shared decision
    stays in one place while the error each surface reports stays its own.
    """


@dataclass(frozen=True)
class ProposalCandidate:
    """One ``issue-<N>-*`` directory and the status it carries.

    ``status`` is None when it could not be read (no ``.status.yaml``, an
    unparseable one, or a caller that did not look because it did not need
    to). None is treated as live — see the module docstring.
    """

    slug: str
    status: str | None = None

    @property
    def is_ignorable(self) -> bool:
        if self.status is None:
            return False
        return self.status.strip().lower() in IGNORABLE_STATUSES


def _describe(candidates: Sequence[ProposalCandidate]) -> str:
    return ", ".join(f"{c.slug} ({c.status or 'status unreadable'})" for c in candidates)


def select_proposal_slug(candidates: Sequence[ProposalCandidate]) -> str | None:
    """The slug to use, or None when this issue has no proposal directory.

    Raises ``AmbiguousProposalError`` when the choice is not forced.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        # One directory is not a choice, so no status read can change the
        # answer — a lone `rejected` proposal still resolves to itself, the
        # way it did before this rule existed.
        return candidates[0].slug

    live = [c for c in candidates if not c.is_ignorable]
    if len(live) == 1:
        return live[0].slug

    if not live:
        raise AmbiguousProposalError(
            f"every proposal directory for this issue is rejected: {_describe(candidates)} "
            "— refusing to resurrect one; create a replacement proposal instead"
        )
    raise AmbiguousProposalError(
        f"multiple live proposal directories for this issue: {_describe(live)} "
        f"(of {_describe(candidates)}) — refusing to guess which one is real"
    )
