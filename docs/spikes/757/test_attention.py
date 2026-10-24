# ruff: noqa: S101
"""Spike #757 scenario and mutation tests (collected via tests/test_spike_757_attention.py)."""

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


env_mod = _load("spike_757_envelope_v1", HERE / "envelope_v1.py")
att = _load("spike_757_attention", HERE / "attention.py")
RULES = att.load_rules(HERE / "rules" / "github_pr.yaml")
REF = "aar_fixture_0001"


class CountingClassifier:
    def __init__(self, answer="CONTINUE"):
        self.calls = 0
        self.answer = answer

    def __call__(self, envelope, observed):
        self.calls += 1
        return self.answer


def fx(name):
    doc = json.loads((HERE / "fixtures" / f"{name}.json").read_text())
    o = doc["observed"]
    return doc["envelope"], (att.ObservedState(**o) if o else None)


def without(rule_id):
    return [r for r in RULES if r["id"] != rule_id]


def digest_of(name):
    e, o = fx(name)
    return att.observed_digest(att.resource_key(e), o)


def run_unchanged(rules, clf=None):
    e, o = fx("unchanged")
    return att.decide(e, digest_of("unchanged"), rules, observed=o, classifier=clf)


def run_ci(rules):
    e, o = fx("ci_failed")
    return att.decide(e, None, rules, observed=o)


def run_ready(rules):
    e, o = fx("ready_to_merge")
    return att.decide(e, None, rules, observed=o, approval_ref=REF)


def run_burst(rules, order=(0, 1)):
    envs, obs = [], None
    for n in ("burst_a", "burst_b"):
        e, obs = fx(n)
        envs.append(e)
    envs = [envs[i] for i in order]
    return att.decide_wake_cycle(envs, {}, rules, observed_by_resource={att.resource_key(envs[0]): obs})


def test_t1_unchanged_digest_is_ignored_without_model():
    clf = CountingClassifier()
    d = run_unchanged(RULES, clf)
    assert (d.decision, d.basis, d.rule_id) == ("IGNORE", "rule", "no_state_change")
    assert clf.calls == 0


def test_t2_ci_failed_notifies():
    d = run_ci(RULES)
    assert (d.decision, d.rule_id) == ("NOTIFY", "ci_failed")


def test_t3_ready_to_merge_requires_approval_and_echoes_ref():
    d = run_ready(RULES)
    assert d.decision == "REQUIRE_APPROVAL"
    assert d.approval_ref == REF


def test_t4_burst_coalesces_to_one_notify():
    out = run_burst(RULES)
    assert len(out) == 1
    assert out[0].decision == "NOTIFY"
    assert out[0].coalesced_event_ids == ("gh-delivery-0004", "gh-delivery-0005")


def test_t5_mutations_turn_each_scenario_red():
    assert run_unchanged(without("no_state_change")).decision != "IGNORE"
    assert run_ci(without("ci_failed")).rule_id != "ci_failed"
    fell = run_ci(without("ci_failed"))
    assert (fell.decision, fell.basis, fell.rule_id) == ("NOTIFY", "model", "unmatched")
    assert run_ready(without("ready_to_merge")).decision != "REQUIRE_APPROVAL"
    # without ci_failed the burst loses its NOTIFY-by-rule (falls to the p3 CONTINUE / unmatched)
    assert run_burst(without("ci_failed"))[0].rule_id != "ci_failed"
    e, o = fx("review_p1")
    assert att.decide(e, None, RULES, observed=o).rule_id == "review_comment_p1"
    assert att.decide(e, None, without("review_comment_p1"), observed=o).rule_id != "review_comment_p1"


def test_t6_duplicates_and_order_do_not_matter():
    a, _ = fx("burst_a")
    _, obs = fx("burst_a")
    key = att.resource_key(a)
    once = att.decide_wake_cycle([a], {}, RULES, observed_by_resource={key: obs})
    twice = att.decide_wake_cycle([a, a], {}, RULES, observed_by_resource={key: obs})
    assert once == twice and len(twice) == 1
    assert run_burst(RULES, (0, 1)) == run_burst(RULES, (1, 0))
    assert run_burst(RULES)[0].decision_id == run_burst(RULES, (1, 0))[0].decision_id


@pytest.mark.parametrize("name", ["unknown_key", "oversize"])
def test_t7_refused_fixtures(name):
    e, _ = fx(name)
    with pytest.raises(env_mod.EnvelopeRefused):
        att.decide(e, None, RULES)
    with pytest.raises(env_mod.EnvelopeRefused):
        att.decide_wake_cycle([e], {}, RULES)


def test_t7_missing_key_refused():
    e, o = fx("ci_failed")
    del e["correlation_id"]
    with pytest.raises(env_mod.EnvelopeRefused):
        att.decide(e, None, RULES, observed=o)


@pytest.mark.parametrize("name", ["unchanged", "ci_failed", "ready_to_merge", "burst_a", "burst_b", "review_p1"])
def test_fixtures_validate(name):
    e, _ = fx(name)
    env_mod.validate(e)


def test_t8_authority_separation():
    from orchestrator import approval_evidence

    assert att.APPROVAL_REF_PATTERN == approval_evidence.GRANT_REF_RE.pattern
    e, o = fx("ready_to_merge")
    with pytest.raises(att.AttentionError):
        att.decide(e, None, RULES, observed=o, approval_ref="grant_123")
    tree = ast.parse((HERE / "attention.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    banned = ("orchestrator", "socket", "http", "urllib", "requests", "httpx", "aiohttp")
    assert not [m for m in imported if m.split(".")[0] in banned]


def test_t9_unmatched_input():
    e, _ = fx("review_p1")
    o2 = att.ObservedState("pending", "blocked", "P2", "a1b2c3d")
    d = att.decide(e, None, RULES, observed=o2)
    assert (d.decision, d.basis, d.rule_id) == ("NOTIFY", "model", "unmatched")
    clf = CountingClassifier("CONTINUE")
    d = att.decide(e, None, RULES, observed=o2, classifier=clf)
    assert clf.calls == 1 and d.decision == "CONTINUE" and d.basis == "model"
    with pytest.raises(att.AttentionError):
        att.decide(e, None, RULES, observed=o2, classifier=CountingClassifier("MAYBE"))


def test_t10_same_head_checks_change_is_not_ignored():
    e, _ = fx("ci_failed")
    pending = att.ObservedState("pending", "blocked", "none", "a1b2c3d")
    failure = att.ObservedState("failure", "blocked", "none", "a1b2c3d")
    key = att.resource_key(e)
    assert att.observed_digest(key, pending) != att.observed_digest(key, failure)
    d = att.decide(e, att.observed_digest(key, pending), RULES, observed=failure)
    assert (d.decision, d.rule_id) == ("NOTIFY", "ci_failed")
    # unknown is never unchanged, even when the digests are equal
    for obs in (None, att.ObservedState("unknown", "blocked", "none", "a1b2c3d")):
        last = att.observed_digest(key, obs)
        d = att.decide(e, last, RULES, observed=obs)
        assert d.rule_id != "no_state_change" and d.decision != "IGNORE"


def test_t11_removed_no_state_change_falls_through():
    d = run_unchanged(without("no_state_change"))
    assert not (d.decision == "IGNORE" and d.rule_id == "no_state_change")


def test_a4_glob_semantics():
    assert att.type_matches("github.pull_request.*", "github.pull_request.opened")
    assert not att.type_matches("github.pull_request.*", "github.pull_request_review.submitted")
    assert att.type_matches("a.b.c", "a.b.c") and not att.type_matches("a.b.c", "a.b.cd")


def test_closed_enums():
    with pytest.raises(att.AttentionError):
        att.ObservedState("green", "clean", "none", "x")
    d = run_ci(RULES)
    with pytest.raises(att.AttentionError):
        type(d)(**{**d.__dict__, "decision": "SHRUG"})
    with pytest.raises(att.AttentionError):
        type(d)(**{**d.__dict__, "basis": "vibes"})


def _pr_event(eid, etype, at, head="a1b2c3d"):
    return {
        "specversion": "mctl.events/v1",
        "id": eid,
        "type": etype,
        "source": "mctl-api",
        "occurred_at": at,
        "correlation_id": "corr-fold",
        "subject": {
            "kind": "github.pull_request",
            "repository": "mctlhq/mctl-agents",
            "number": "123",
            "head_sha": head,
        },
    }


def test_new_head_sha_with_same_state_is_not_ignored():
    # A new push with the same checks/mergeable/severity is a state change, not a no-op.
    e, _ = fx("ci_failed")
    key = att.resource_key(e)
    old = att.ObservedState("failure", "blocked", "none", "a1b2c3d")
    new = att.ObservedState("failure", "blocked", "none", "e4f5a6b")
    d = att.decide(e, att.observed_digest(key, old), RULES, observed=new)
    assert d.rule_id != "no_state_change" and d.decision != "IGNORE"


def test_wake_cycle_folds_to_highest_severity_regardless_of_position():
    # The lower-severity event (review P3 -> CONTINUE) sorts first; the fold must still pick NOTIFY.
    review = _pr_event("gh-delivery-0101", "github.pull_request_review.submitted", "2026-10-24T10:00:00Z")
    push = _pr_event("gh-delivery-0102", "github.pull_request.synchronize", "2026-10-24T10:01:00Z")
    obs = att.ObservedState("failure", "blocked", "P3", "a1b2c3d")
    key = att.resource_key(push)
    assert att.decide(review, None, RULES, observed=obs).decision == "CONTINUE"
    for order in ([review, push], [push, review]):
        out = att.decide_wake_cycle(order, {}, RULES, observed_by_resource={key: obs})
        assert [(d.decision, d.rule_id) for d in out] == [("NOTIFY", "ci_failed")]
        assert out[0].coalesced_event_ids == ("gh-delivery-0101", "gh-delivery-0102")
        assert out[0].decided_at == "2026-10-24T10:01:00Z"
    a = att.decide_wake_cycle([review, push], {}, RULES, observed_by_resource={key: obs})
    b = att.decide_wake_cycle([push, review], {}, RULES, observed_by_resource={key: obs})
    assert a == b


@pytest.mark.parametrize(
    "subject",
    [
        {"kind": "github.pull_request", "repository": "mctlhq/mctl-agents"},
        {"kind": "github.pull_request", "number": "123"},
        {"kind": "github.issue", "repository": "mctlhq/mctl-agents", "number": "123"},
    ],
)
def test_envelope_without_pr_identity_is_refused(subject):
    e = {**_pr_event("gh-delivery-0201", "github.pull_request.opened", "2026-10-24T10:00:00Z"), "subject": subject}
    with pytest.raises(env_mod.EnvelopeRefused):
        att.decide(e, None, RULES, observed=att.ObservedState("failure", "blocked", "none", "a1b2c3d"))


def test_impossible_occurred_at_is_refused():
    e = _pr_event("gh-delivery-0301", "github.pull_request.opened", "2026-02-30T10:00:00Z")
    with pytest.raises(env_mod.EnvelopeRefused):
        env_mod.validate(e)


def test_decision_id_differs_when_classifier_outcomes_differ():
    e, o = fx("ci_failed")
    rules = without("ci_failed")
    a = att.decide(e, None, rules, observed=o, classifier=CountingClassifier("NOTIFY"))
    b = att.decide(e, None, rules, observed=o, classifier=CountingClassifier("CONTINUE"))
    assert (a.rule_id, b.rule_id) == ("unmatched", "unmatched")
    assert a.decision_id != b.decision_id
    again = att.decide(e, None, rules, observed=o, classifier=CountingClassifier("NOTIFY"))
    assert again.decision_id == a.decision_id


def test_wake_cycle_orders_by_instant_not_string():
    # 10:30+02:00 is 08:30Z, earlier than 09:00Z, although it sorts later as text.
    early = _pr_event("gh-delivery-0401", "github.pull_request_review.submitted", "2026-10-24T10:30:00+02:00")
    late = _pr_event("gh-delivery-0402", "github.pull_request.synchronize", "2026-10-24T09:00:00Z")
    late["correlation_id"] = "corr-late"
    obs = att.ObservedState("failure", "blocked", "P3", "a1b2c3d")
    key = att.resource_key(late)
    for order in ([early, late], [late, early]):
        (d,) = att.decide_wake_cycle(order, {}, RULES, observed_by_resource={key: obs})
        assert d.decided_at == "2026-10-24T09:00:00Z"
        assert d.subject_ref == "corr-late"
