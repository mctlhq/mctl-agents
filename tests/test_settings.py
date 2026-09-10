"""Tests that keep config/settings.py's service registries true.

SERVICES, NON_ROTATING_SERVICES, and ROTATING_SERVICES are hand-maintained
collections that gate which services the implementer/investigator and the
proactive rotation operate on. A service added to one without the other
either breaks the rotation (no scaffold, proactive run fails) or silently
drops a valid implementer target — these tests catch that drift.
"""
from __future__ import annotations

from config.settings import NON_ROTATING_SERVICES, ROTATING_SERVICES, SERVICES


def test_portfolio_is_a_registered_service():
    """portfolio must be a valid implementer/investigator target."""
    assert "portfolio" in SERVICES


def test_portfolio_is_non_rotating():
    """portfolio has no agents/portfolio/ scaffold, so it must stay out of the rotation."""
    assert "portfolio" in NON_ROTATING_SERVICES
    assert "portfolio" not in ROTATING_SERVICES


def test_non_rotating_services_are_all_registered():
    """A non-rotating service that is not also a registered service is a dangling entry."""
    assert set(NON_ROTATING_SERVICES) <= set(SERVICES)


def test_services_has_no_duplicates():
    """A duplicated entry in SERVICES silently double-counts a service in derived sets."""
    assert len(SERVICES) == len(set(SERVICES))
