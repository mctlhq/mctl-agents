#!/usr/bin/env python3
"""The Paid Capabilities proof (mctlhq/mctl-agents#620, C5; ADR 020).

One tenant, simulated EUR: a EUR 0.12 `per_successful_call` pilot capability
against a EUR 1.00 budget, through every fault and denial case in the issue.

    uv run python tools/commerce_proof.py --backend offline --out evidence/commerce-proof.json
    uv run python tools/commerce_proof.py --backend live    --out evidence/commerce-proof.json

- `offline` (CI) runs against the in-memory `FakeLedger` and `FakeApprovals`
  from `tests/commerce_fakes.py` plus the real `ProviderIngress` on a
  temporary sqlite file. Deterministic; proves the gateway and the provider.
  It proves nothing about mctl-api's own atomicity.
- `live` uses a real `CommerceClient` against mctl-api for one proof tenant
  (simulated credits only), the real in-process pilot ingress, and the real
  dispatch keys. Operator-run. Needs `MCTL_TOKEN` (tenant admin of the proof
  tenant), `MCTL_COMMERCE_TENANT_ID`, and for S7 a second tenant's token in
  `MCTL_TOKEN_OTHER_TENANT` with `MCTL_OTHER_TENANT_ID`. Scenarios that need
  a fault injected into mctl-api, a test hook or a human approval are not
  driven live and are recorded as `skipped` with the reason (S11:
  `offline-only`), never as passed live. Live scenarios share one tenant and
  one budget: each sees only the invocations it created (rows listed since
  its start, minus the ones that already existed) and compares budgets as
  deltas. S1 and S2 need an unspent EUR 1.00 budget and are skipped
  otherwise; run each with `--only` on a fresh budget.

Every scenario function is pure over an `Env` (a way to get a fresh world);
the I/O wrappers are `main()` and `write_bundle()`. The evidence bundle names
the commit SHAs, image tags, encoding versions, terms id and version, and the
expected and observed result of each scenario. It never contains arguments,
results, tokens or payment proofs.

Owner decisions of 2026-10-11 (the proposal's task list): the proof tenant is
a dedicated `commerce-proof` tenant, never `admins`; the accept-only kill
switch answers `unsupported-execution`; the settlement-only retry (S11) is
proven offline only and the bundle says so.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator import action_approvals as aa  # noqa: E402
from orchestrator import commerce as cm  # noqa: E402
from orchestrator import commerce_gateway as cg  # noqa: E402
from orchestrator import commerce_metrics  # noqa: E402
from orchestrator import commerce_provider as cp  # noqa: E402
from orchestrator import policy_checkpoint as pc  # noqa: E402

BUNDLE_SCHEMA_VERSION = 1
PRICE_MINOR = 12
BUDGET_MINOR = 100
OFFLINE_ONLY_SETTLEMENT = "offline-only (owner decision 2026-10-11)"
#: Authoritative for S14's verdict, so it is the gateway's own set.
TERMINAL = cg.TERMINAL_PHASES
#: Drives only S14's tenant-wide cross-check reads; a stale entry costs a
#: missed cross-check, never a wrong verdict.
NON_TERMINAL = ("accepted", "reserved", "dispatching", "dispatched", "outcome_unknown")
#: `CommerceClient.list_invocations` caps a page here; a full page is a
#: truncated read, never a complete one.
PAGE = 200
#: A live scenario lists rows created since this long before it started.
LOOKBACK_S = 120
#: Scenarios that need an unspent EUR 1.00 budget.
FRESH_BUDGET = frozenset({"S1", "S2"})
#: Why each scenario is not driven against mctl-api.
LIVE_NOT_DRIVEN: dict[str, str] = {
    "S8": "needs the grant revoked between two ledger calls; that hook exists only in the offline fake",
    "S9": "needs a human approval decision mid-run; not automated against the live approval store",
    "S10": "needs the gateway killed after specific ledger calls; that hook exists only in the offline fake",
    "S11": OFFLINE_ONLY_SETTLEMENT,
    "S12": "replays S1, S3-S5 and S8-S11 twice: it would spend the live budget again and needs the offline hooks",
    "S13": "needs the accept-only switch flipped and the reconciler driven mid-run: an operator procedure "
           "(docs/runbooks/commerce-enable-disable.md), with no fault hook in mctl-api (owner decision 2026-10-11)",
}

PASSED, FAILED, SKIPPED, OFFLINE_ONLY = "passed", "failed", "skipped", "offline-only"


class ProofError(RuntimeError):
    """The proof could not observe what it needs (never a pass)."""


class Skip(Exception):
    """A precondition of this backend does not hold: recorded as skipped."""


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------


@dataclass
class Env:
    """Where a scenario runs. `new_world()` yields a world (a `World` from
    tests/commerce_fakes.py) with the pilot terms published."""

    name: str
    new_world: Callable[[], contextlib.AbstractContextManager[Any]]
    worlds: list[Any] = field(default_factory=list)
    #: Scenario id -> why this backend does not drive it.
    not_driven: Mapping[str, str] = field(default_factory=dict)
    order: tuple[str, ...] = ()
    other_tenant: str = "tenant-b"
    other_ledger: Any = None
    #: Invocation ids every closed world created (`_close`), for S14.
    created: set[str] = field(default_factory=set)
    #: Why a world's invocations could not be read on close; fails S14.
    created_unreadable: list[str] = field(default_factory=list)


def _fakes() -> Any:
    from tests import commerce_fakes

    return commerce_fakes


def offline_env() -> Env:
    fk = _fakes()
    env = Env(name="offline", new_world=contextlib.nullcontext, order=ORDER)

    @contextlib.contextmanager
    def new_world() -> Iterator[Any]:
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            clock = fk.Clock()
            world = fk.World(
                clock=clock, ledger=fk.FakeLedger(clock, price_mode="per_successful_call", amount=PRICE_MINOR,
                                                  budget=BUDGET_MINOR),
                approvals=fk.FakeApprovals(clock), tmp=Path(tmp),
                statuses={f"OPS-{i}": "open" for i in range(1, 60)},
            )
            world.ledger.publish(fk.PILOT_CAP, world.schema_hash())
            stack.enter_context(mock.patch.object(
                pc, "current_identity",
                lambda: pc.ExecutionIdentity(execution_id=fk.EXECUTION, trace_id="tr-proof", actor="agent:proof")))
            stack.enter_context(mock.patch.object(aa, "ttl_s", lambda: 3600))
            _mark(world)
            env.worlds.append(world)
            try:
                yield world
            finally:
                _close(env, world)

    env.new_world = new_world
    env.other_ledger = None
    return env


def live_env() -> Env:
    """Operator-run. Reads its configuration from the environment and fails
    loudly, before any call, when something is missing."""
    fk = _fakes()
    tenant = os.environ.get("MCTL_COMMERCE_TENANT_ID", "").strip()
    if not tenant or tenant == "admins":
        raise ProofError("MCTL_COMMERCE_TENANT_ID must name the dedicated proof tenant (never 'admins')")
    client = cm.CommerceClient()
    answer = client.dispatch_keys()
    if not answer.ok:
        raise ProofError(f"dispatch keys could not be read: {answer.reason}")
    keys = _public_keys(answer.body)
    worlds_opened = itertools.count()
    other = None
    other_token = os.environ.get("MCTL_TOKEN_OTHER_TENANT", "").strip()
    if other_token:
        other = cm.CommerceClient(token=other_token)
    env = Env(
        name="live", new_world=contextlib.nullcontext, other_tenant=os.environ.get("MCTL_OTHER_TENANT_ID", "tenant-b"),
        not_driven=LIVE_NOT_DRIVEN, other_ledger=other,
        # Scenarios that need headroom first; S1 and S2 then run only on an
        # unspent budget (see the module docstring).
        order=("S6", "S7", "S3", "S4", "S5", "S1", "S2", *(f"S{i}" for i in range(8, 15))),
    )

    class _RealClock:
        def __call__(self) -> datetime:
            return datetime.now(UTC)

    @contextlib.contextmanager
    def new_world() -> Iterator[Any]:
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            world = fk.World(
                clock=_RealClock(), ledger=client, approvals=aa.ActionApprovalClient(), tmp=Path(tmp),
                statuses={f"OPS-{i}": "open" for i in range(1, 60)}, tenant=tenant, public_keys=keys,
            )
            # One execution id per world: two scenarios sharing one would
            # share idempotency keys (S7 and S1 both call OPS-1).
            execution_id = "we_" + _world_nonce(next(worlds_opened))
            stack.enter_context(mock.patch.object(
                pc, "current_identity",
                lambda: pc.ExecutionIdentity(execution_id=execution_id, trace_id="tr-proof", actor="agent:proof")))
            _mark(world)
            env.worlds.append(world)
            try:
                yield world
            finally:
                _close(env, world)

    env.new_world = new_world
    return env


def _world_nonce(counter: int) -> str:
    """26 uppercase hex characters, unique per world and per run: the run's
    start in milliseconds, then the world's index."""
    return (format(int(time.time() * 1000), "X") + format(counter, "04X")).rjust(26, "0")[-26:]


def _public_keys(body: Mapping[str, Any]) -> dict[str, bytes]:
    """`{"keys": [{"key_id": ..., "public_key": <base64url ed25519>}]}`."""
    out: dict[str, bytes] = {}
    rows = body.get("keys")
    if not isinstance(rows, list) or not rows:
        raise ProofError("dispatch-keys carried no keys")
    for row in rows:
        try:
            out[str(row["key_id"])] = cm._unb64url(str(row["public_key"]))
        except (KeyError, TypeError, cm.CommerceError) as exc:
            raise ProofError(f"unreadable dispatch key: {type(exc).__name__}") from exc
    return out


# ---------------------------------------------------------------------------
# Helpers shared by scenarios
# ---------------------------------------------------------------------------


def _cap() -> str:
    return _fakes().PILOT_CAP


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _budget(world: Any) -> dict[str, Any]:
    """`BudgetView.to_dict()`: the `*_minor` fields, no timestamp."""
    answer = world.ledger.get_budget(world.tenant)
    if not answer.ok:
        raise ProofError(f"budget could not be read: {answer.status} {answer.reason}")
    return dict(answer.body)


def _list(world: Any, *, phase: str | None = None, since: str | None = None) -> list[dict[str, Any]]:
    answer = world.ledger.list_invocations(world.tenant, phase=phase, since=since, limit=PAGE)
    if not answer.ok:
        raise ProofError(f"invocations could not be listed: {answer.status} {answer.reason}")
    rows = list(answer.body["invocations"])
    if len(rows) >= PAGE:
        raise ProofError(f"the invocation list filled a page of {PAGE} (phase={phase!r}, since={since!r}); "
                         "a truncated read cannot prove completeness")
    return rows


def _mark(world: Any) -> None:
    """Scope a world to the invocations it creates: on a shared live tenant,
    rows that existed when it opened are never this scenario's."""
    world.proof_since = cm.hashed_time(world.clock() - timedelta(seconds=LOOKBACK_S))
    world.proof_preexisting = frozenset(r["invocation_id"] for r in _list(world, since=world.proof_since))


def _close(env: Env, world: Any) -> None:
    """On every exit of a world (pass, failure or `Skip`), record the
    invocations it created for S14, whatever its scenario passed as
    `receipts=`. Never raises: a failed bookkeeping read must not turn a
    scenario whose checks held into a failure, or mask the exception it is
    unwinding. The read failure is S14's instead, the scenario the ids are
    for."""
    try:
        env.created.update(str(r["invocation_id"]) for r in _rows(world))
    except Exception as exc:  # noqa: BLE001 — recorded for S14, never raised here
        env.created_unreadable.append(f"{type(exc).__name__}: {exc}")


def _rows(world: Any) -> list[dict[str, Any]]:
    """The invocations this world created, oldest first."""
    rows = [r for r in _list(world, since=world.proof_since)
            if r["invocation_id"] not in world.proof_preexisting]
    return sorted(rows, key=lambda r: (r.get("created_at", ""), r["invocation_id"]))


def _need_headroom(budget: Mapping[str, Any], minor: int) -> None:
    if budget["available_minor"] < minor:
        raise Skip(f"needs at least {minor} minor available (available_minor={budget['available_minor']})")


def _need_unspent(budget: Mapping[str, Any], sid: str) -> None:
    if (budget["available_minor"] != BUDGET_MINOR or budget["reserved_minor"]
            or budget["pending_liability_minor"]):
        raise Skip(f"needs an unspent EUR 1.00 budget (available_minor={budget['available_minor']}, "
                   f"reserved_minor={budget['reserved_minor']}); reset it and rerun with --only {sid}")


def _receipt(world: Any, invocation_id: str) -> dict[str, Any]:
    answer = world.ledger.get_receipt(world.tenant, invocation_id)
    if not answer.ok:
        raise ProofError(f"receipt could not be read: {answer.status} {answer.reason}")
    return dict(answer.body)


def _call(world: Any, ticket: str, key: str | None = None, gateway: Any = None) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        g = gateway or await world.gateway()
        return await g.invoke(_cap(), {"ticket": ticket}, key)

    return _run(go())


def _captured(world: Any) -> int:
    return sum(1 for r in _rows(world) if r.get("charge_status") == "captured")


def _answer_shape(answer: cm.CommerceAnswer) -> tuple[Any, ...]:
    return (answer.status, answer.http_status, answer.code, answer.reason, dict(answer.body))


@dataclass
class Result:
    id: str
    name: str
    status: str
    expected: dict[str, Any]
    observed: dict[str, Any]
    checks: list[dict[str, Any]]
    #: Informational: the receipts a scenario chose to cite. S14 checks
    #: `Env.created`, every row every world wrote, not these.
    receipt_ids: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "status": self.status, "expected": self.expected,
                "observed": self.observed, "checks": self.checks, "receipt_ids": sorted(self.receipt_ids),
                "note": self.note}


def _finish(sid: str, name: str, expected: dict[str, Any], observed: dict[str, Any],
            checks: list[tuple[str, bool]], *, receipts: list[str] | None = None, note: str = "",
            status_if_ok: str = PASSED) -> Result:
    ok = all(passed for _label, passed in checks)
    return Result(sid, name, status_if_ok if ok else FAILED, expected, observed,
                  [{"check": label, "passed": passed} for label, passed in checks], receipts or [], note)


# ---------------------------------------------------------------------------
# Scenarios. Each takes an Env and returns a Result.
# ---------------------------------------------------------------------------

SCENARIOS: dict[str, Callable[[Env], Result]] = {}
NAMES: dict[str, str] = {}


def scenario(sid: str, name: str) -> Callable[[Callable[[Env], Result]], Callable[[Env], Result]]:
    def register(fn: Callable[[Env], Result]) -> Callable[[Env], Result]:
        SCENARIOS[sid] = fn
        NAMES[sid] = name
        return fn

    return register


@scenario("S1", "12 concurrent distinct calls against a EUR 1.00 budget")
def s1(env: Env) -> Result:
    with env.new_world() as w:
        before = _budget(w)
        _need_unspent(before, "S1")

        async def go() -> list[dict[str, Any]]:
            g = await w.gateway()
            return list(await asyncio.gather(*[g.invoke(_cap(), {"ticket": f"OPS-{i}"}) for i in range(1, 13)]))

        outs = _run(go())
        after = _budget(w)
        ok = [o for o in outs if o["reason_code"] == "ok"]
        refused = [o for o in outs if o["reason_code"] == "budget-exhausted"]
        spent = after["committed_minor"] - before["committed_minor"]
        observed = {"calls": len(outs), "captured": _captured(w), "budget_exhausted": len(refused),
                    "other": len(outs) - len(ok) - len(refused), "committed": spent,
                    "reserved": after["reserved_minor"], "available": after["available_minor"],
                    "executions": len(w.executions)}
        return _finish("S1", NAMES["S1"],
                       {"captured_at_most": 8, "committed_at_most": 96, "available_at_least": 4,
                        "all_others": "budget-exhausted"}, observed, [
            ("at most 8 captured", observed["captured"] <= 8 and len(ok) <= 8),
            ("every other call is budget-exhausted", observed["other"] == 0),
            ("this run committed <= 96 and available >= 4", spent <= 96 and after["available_minor"] >= 4),
            ("each capture committed exactly the price", spent == PRICE_MINOR * observed["captured"]),
            ("nothing left reserved", after["reserved_minor"] == 0),
            ("provider ran once per captured call", observed["executions"] == len(ok)),
        ], receipts=[r["invocation_id"] for r in _rows(w)])


@scenario("S2", "the 9th sequential invocation after 8 captures")
def s2(env: Env) -> Result:
    with env.new_world() as w:
        before = _budget(w)
        _need_unspent(before, "S2")
        n = 1
        while _budget(w)["available_minor"] >= PRICE_MINOR and n < 40:
            _call(w, f"OPS-{n}")
            n += 1
        captured_before = _captured(w)
        out = _call(w, f"OPS-{n}")
        after = _budget(w)
        observed = {"captured_before_ninth": captured_before, "ninth": out["reason_code"],
                    "committed": after["committed_minor"] - before["committed_minor"]}
        return _finish("S2", NAMES["S2"], {"ninth": "budget-exhausted", "committed": 96}, observed, [
            ("8 captures before the 9th", captured_before == 8),
            ("the 9th is budget-exhausted", out["reason_code"] == "budget-exhausted"),
            ("this run committed 96 minor", observed["committed"] == 96),
        ], receipts=[r["invocation_id"] for r in _rows(w)])


@scenario("S3", "provider failure releases the reservation")
def s3(env: Env) -> Result:
    with env.new_world() as w:
        before = _budget(w)
        _need_headroom(before, PRICE_MINOR)
        out = _call(w, "OPS-404")  # the pilot records an unknown ticket as a failure
        after = _budget(w)
        rows = _rows(w)
        observed = {"reason_code": out["reason_code"], "available_before": before["available_minor"],
                    "available_after": after["available_minor"],
                    "committed": after["committed_minor"] - before["committed_minor"],
                    "charge_status": rows[-1].get("charge_status") if rows else None}
        return _finish("S3", NAMES["S3"], {"reason_code": "provider-error", "available_restored": True}, observed, [
            ("the call fails", out["reason_code"] == "provider-error"),
            ("available returns to its prior value", after["available_minor"] == before["available_minor"]),
            ("nothing committed", observed["committed"] == 0),
            ("the charge is released", observed["charge_status"] == "released"),
        ], receipts=[r["invocation_id"] for r in rows])


@scenario("S4", "the same key and arguments retried")
def s4(env: Env) -> Result:
    with env.new_world() as w:
        _need_headroom(_budget(w), PRICE_MINOR)
        first = _call(w, "OPS-1", "retry-1")
        second = _call(w, "OPS-1", "retry-1")
        rows = _rows(w)
        observed = {"same_invocation": first.get("invocation_id") == second.get("invocation_id"),
                    "invocations": len(rows), "captures": _captured(w), "executions": len(w.executions)}
        return _finish("S4", NAMES["S4"], {"invocations": 1, "captures": 1, "executions": 1}, observed, [
            ("the original invocation is returned", bool(observed["same_invocation"])),
            ("one invocation", observed["invocations"] == 1),
            ("one capture", observed["captures"] == 1),
            ("one provider execution", observed["executions"] == 1),
        ], receipts=[r["invocation_id"] for r in rows])


@scenario("S5", "the same idempotency key reused with changed arguments")
def s5(env: Env) -> Result:
    """The gateway derives the ledger key from the arguments' digest, so a
    changed argument set through `capability_invoke` is a different call.
    The conflict is mctl-api's, and is proven at the ledger: the key the
    first call was accepted under, presented with a quote for other
    arguments."""
    fk = _fakes()
    with env.new_world() as w:
        _need_headroom(_budget(w), PRICE_MINOR)
        first = _call(w, "OPS-1", "k5")
        rows_before = _rows(w)
        receipt_before = _receipt(w, first["invocation_id"])
        key = cm.gateway_idempotency_key(
            execution_id=pc.current_identity().execution_id, capability_id=fk.PILOT_CAP,
            arguments_digest=cm.args_digest({"ticket": "OPS-1"}), client_key="k5")
        quote = w.ledger.mint_quote(
            w.tenant, capability_id=fk.PILOT_CAP, input_schema_hash=w.schema_hash(),
            args_digest=cm.args_digest({"ticket": "OPS-2"}), execution_id=pc.current_identity().execution_id)
        conflict = w.ledger.accept(w.tenant, quote_doc=dict(quote.body), idempotency_key=key, approval_id="",
                                   terms_binding_value="") if quote.ok else quote
        receipt_after = _receipt(w, first["invocation_id"])
        observed = {"first": first["reason_code"], "conflict": conflict.code,
                    "invocations": len(_rows(w)), "original_unchanged": receipt_after == receipt_before}
        return _finish("S5", NAMES["S5"], {"conflict": "idempotency-conflict", "original_unchanged": True}, observed, [
            ("the first call succeeded", first["reason_code"] == "ok"),
            ("the changed-argument accept is refused with idempotency-conflict",
             conflict.status == cm.REFUSED and conflict.code == "idempotency-conflict"),
            ("the original invocation is unchanged", receipt_after == receipt_before),
            ("no second invocation", len(_rows(w)) == len(rows_before)),
        ], receipts=[first["invocation_id"]])


@scenario("S6", "the provider called directly without a valid dispatch authorization")
def s6(env: Env) -> Result:
    fk = _fakes()
    with env.new_world() as w:
        ingress = w.new_ingress()
        forged_seed = b"\x01" * 32
        forged = cm.sign_dispatch_authorization(forged_seed, cm.DispatchAuthorization(
            key_id=next(iter(w.public_keys or {fk.KID: b""})), invocation_id="inv_forged",
            provider_operation_key=cm.provider_operation_key("inv_forged"), provider_id=cp.PILOT_PROVIDER_ID,
            capability_id=fk.PILOT_CAP, input_schema_hash=w.schema_hash(),
            args_digest=cm.args_digest({"ticket": "OPS-1"}), tenant_id=w.tenant,
            dispatch_deadline=cm.hashed_time(w.clock()),
        ))
        session = cp.PaidLocalProviderSession(ingress)
        attempts = {
            "none": lambda: session.call_tool(cp.PILOT_TOOL, {"ticket": "OPS-1"}),
            "forged": lambda: ingress.call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, forged),
        }
        refusals: dict[str, str] = {}
        for label, attempt in attempts.items():
            try:
                _run(attempt())
                refusals[label] = "executed"
            except cm.DispatchRefused:
                refusals[label] = "refused"
        observed = {"refusals": refusals, "executions": len(w.executions)}
        return _finish("S6", NAMES["S6"], {"refusals": {"none": "refused", "forged": "refused"}, "executions": 0},
                       observed, [
            ("a call without an authorization is refused", refusals["none"] == "refused"),
            ("a forged authorization is refused", refusals["forged"] == "refused"),
            ("the provider executed nothing", not w.executions),
        ])


@scenario("S7", "a second tenant reads and invokes against the first tenant's invocation and budget")
def s7(env: Env) -> Result:
    with env.new_world() as w:
        if env.name == "live" and env.other_ledger is None:
            raise Skip("MCTL_TOKEN_OTHER_TENANT is not set")
        _need_headroom(_budget(w), PRICE_MINOR)
        out = _call(w, "OPS-1")
        inv_id = out["invocation_id"]
        budget_before, rows_before = _budget(w), _rows(w)
        other = env.other_ledger or w.ledger
        b = env.other_tenant
        foreign = other.get_receipt(b, inv_id)
        missing = other.get_receipt(b, "inv_00000000000000000000000000")
        own_missing = w.ledger.get_receipt(w.tenant, "inv_00000000000000000000000000")
        foreign_budget = other.get_budget(b)
        missing_budget = other.get_budget("tenant-nobody")
        foreign_list = other.list_invocations(b)
        routes = {name: getattr(other, name)(b, inv_id) for name in ("reserve", "dispatch", "get_invocation")}
        routes["release"] = other.release(b, inv_id, reason_code="provider-error")
        same = _answer_shape(foreign) == _answer_shape(missing) == _answer_shape(own_missing)
        w_b = _run(_other_invoke(env, w, b, other))
        budget_after, rows_after = _budget(w), _rows(w)
        observed = {
            "foreign_equals_missing": same, "receipt_code": foreign.code,
            "budget_read_code": foreign_budget.code, "budget_matches_missing": _answer_shape(foreign_budget)
            == _answer_shape(missing_budget), "list_code": foreign_list.code,
            "write_routes": {k: v.code for k, v in routes.items()}, "invoke_reason": w_b,
            "state_unchanged": budget_after == budget_before and rows_after == rows_before,
        }
        return _finish("S7", NAMES["S7"], {"read_code": "not-found", "state_unchanged": True}, observed, [
            ("a foreign invocation and a missing one answer identically", same),
            ("the receipt read is not-found", foreign.code == "not-found"),
            ("the budget read is not-found, same as a nonexistent tenant",
             foreign_budget.code == "not-found" and observed["budget_matches_missing"]),
            ("the list read is not-found", foreign_list.code == "not-found"),
            ("every write route answers not-found", all(v.code == "not-found" for v in routes.values())),
            ("an invoke by the other tenant is refused", w_b != "ok"),
            ("nothing changed", observed["state_unchanged"]),
        ], receipts=[inv_id])


async def _other_invoke(env: Env, w: Any, other_tenant: str, other_ledger: Any) -> str:
    fk = _fakes()
    w2 = fk.World(clock=w.clock, ledger=other_ledger, approvals=w.approvals, tmp=w.tmp, statuses=w.statuses,
                  tenant=other_tenant, public_keys=w.public_keys)
    g = await w2.gateway()
    return str((await g.invoke(_cap(), {"ticket": "OPS-1"}))["reason_code"])


@contextlib.contextmanager
def _revoked_after(world: Any, route: str) -> Iterator[None]:
    state = {"on": False}
    real = pc.evaluate

    def evaluate(policy: Any, request: Any) -> Any:
        if state["on"]:
            return None, pc.CODE_NO_RULE, "revoked"
        return real(policy, request)

    with mock.patch.object(pc, "evaluate", evaluate):
        world.ledger.hooks[route] = lambda: state.update(on=True)
        yield


@scenario("S8", "permission revoked between acceptance and dispatch")
def s8(env: Env) -> Result:
    observed: dict[str, Any] = {}
    checks: list[tuple[str, bool]] = []
    for route, expected_phase in (("accept", "refused"), ("reserve", "released")):
        with env.new_world() as w, _revoked_after(w, route):
            before = _budget(w)
            out = _call(w, "OPS-1")
            rows = _rows(w)
            after = _budget(w)
            phase = rows[0]["phase"] if rows else None
            observed[f"after_{route}"] = {"reason_code": out["reason_code"], "phase": phase,
                                          "dispatched": "dispatch" in w.ledger.calls,
                                          "executions": len(w.executions)}
            checks += [
                (f"revoked after {route}: policy-denied", out["reason_code"] == "policy-denied"),
                (f"revoked after {route}: phase {expected_phase}", phase == expected_phase),
                (f"revoked after {route}: no dispatch, no execution",
                 "dispatch" not in w.ledger.calls and not w.executions),
                (f"revoked after {route}: funds restored", after == before),
            ]
    return _finish("S8", NAMES["S8"], {"after_accept": "refused", "after_reserve": "released"}, observed, checks)


@scenario("S9", "approval pending holds no funds, denial refuses with no reservation")
def s9(env: Env) -> Result:
    fk = _fakes()
    with env.new_world() as w:
        w.policy = fk.APPROVE_PAID
        before = _budget(w)
        first = _call(w, "OPS-1")
        pending = _budget(w)
        w.approvals.decide(first["approval_ref"], "denied")
        second = _call(w, "OPS-1")
        after = _budget(w)
        observed = {"pending": first["reason_code"], "reserved_while_pending": pending["reserved_minor"],
                    "after_denial": second["reason_code"], "invocations": len(_rows(w)),
                    "executions": len(w.executions), "reserved_after": after["reserved_minor"]}
        return _finish("S9", NAMES["S9"], {"reserved_while_pending": 0, "after_denial": "policy-denied"}, observed, [
            ("a pending approval needs a human", bool(first.get("approval_ref"))),
            ("no funds held while pending", pending["reserved_minor"] == 0 and pending == before),
            ("denial refuses the call", second["reason_code"] == "policy-denied"),
            ("no invocation and no reservation after denial", observed["invocations"] == 0
             and after["reserved_minor"] == 0),
            ("the provider executed nothing", not w.executions),
        ])


@scenario("S10", "gateway killed at each point after acceptance, then restarted")
def s10(env: Env) -> Result:
    fk = _fakes()
    observed: dict[str, Any] = {}
    checks: list[tuple[str, bool]] = []
    points = (("after_accept", "accept", "crash-after"), ("after_reserve", "reserve", "crash-after"),
              ("after_fence", "dispatch", "crash-after"), ("after_provider_ran", "transition", "crash-before"))
    for label, route, mode in points:
        with env.new_world() as w:
            w.ledger.inject(route, mode)
            crashed = False
            try:
                _call(w, "OPS-1")
            except fk.Crash:
                crashed = True
            if label == "after_fence":
                w.clock.advance(60 + 30 + 31)  # past deadline + skew: not_found is definitive
            out = _call(w, "OPS-1")
            b = _budget(w)
            observed[label] = {"crashed": crashed, "after_restart": out["reason_code"],
                               "executions": len(w.executions), "committed": b["committed_minor"],
                               "reserved": b["reserved_minor"]}
            checks += [
                (f"{label}: the kill happened", crashed),
                (f"{label}: the provider ran at most once", len(w.executions) <= 1),
                (f"{label}: captured at most once", b["committed_minor"] in (0, PRICE_MINOR)),
                (f"{label}: nothing left reserved", b["reserved_minor"] == 0),
            ]
            if label != "after_fence":
                checks.append((f"{label}: resumed to a captured success",
                               out["reason_code"] == "ok" and b["committed_minor"] == PRICE_MINOR))
    return _finish("S10", NAMES["S10"], {"executions_at_most": 1, "captures_at_most": 1}, observed, checks)


@scenario("S11", "capture settlement unknown after a confirmed success")
def s11(env: Env) -> Result:
    with env.new_world() as w:
        w.ledger.settlement_unknown = 1
        out = _call(w, "OPS-1")
        rid = out["invocation_id"]
        pending = _receipt(w, rid)
        dispatches = w.ledger.calls.count("dispatch")
        again = _call(w, "OPS-1")  # a retry: never re-dispatches
        settled_n = w.ledger.settle_pending()
        settled = _receipt(w, rid)
        observed = {"first": out["reason_code"], "settlement_before": pending["settlement_status"],
                    "retry": again["reason_code"], "settled": settled_n,
                    "settlement_after": settled["settlement_status"], "executions": len(w.executions),
                    "dispatches": w.ledger.calls.count("dispatch"), "committed": _budget(w)["committed_minor"]}
        return _finish("S11", NAMES["S11"], {"settlement_before": "pending", "settlement_after": "settled",
                                             "executions": 1}, observed, [
            ("the call succeeded", out["reason_code"] == "ok"),
            ("settlement stays pending", pending["settlement_status"] == "pending"),
            ("a retry never re-dispatches", observed["dispatches"] == dispatches == 1),
            ("the settlement-only retry settles it", settled["settlement_status"] == "settled"),
            ("the provider ran once and the charge was committed once",
             observed["executions"] == 1 and observed["committed"] == PRICE_MINOR),
        ], receipts=[rid], note=OFFLINE_ONLY_SETTLEMENT, status_if_ok=OFFLINE_ONLY)


@scenario("S12", "telemetry export on and off give identical ledgers")
def s12(env: Env) -> Result:
    recorded: list[tuple[str, dict[str, str]]] = []

    class Recorder:
        def add(self, name: str, value: int, attributes: Mapping[str, str]) -> None:
            recorded.append((name, dict(attributes)))

        def record(self, name: str, value: float, attributes: Mapping[str, str]) -> None:
            recorded.append((name, dict(attributes)))

    comparable = ("S1", "S3", "S4", "S5", "S8", "S9", "S10", "S11")
    runs: dict[str, dict[str, Any]] = {}
    for label, backend in (("on", Recorder()), ("off", None)):
        commerce_metrics.set_backend(backend)
        try:
            runs[label] = {sid: SCENARIOS[sid](env).observed for sid in comparable}
        finally:
            commerce_metrics.set_backend(None)
    keys = {key for _name, attrs in recorded for key in attrs}
    observed = {"compared": list(comparable), "identical": runs["on"] == runs["off"],
                "metric_points": len(recorded), "attribute_keys": sorted(keys)}
    return _finish("S12", NAMES["S12"], {"identical": True, "attribute_keys_within": sorted(
        commerce_metrics.ALLOWED_ATTRIBUTES)}, observed, [
        ("ledger totals and receipts are identical with telemetry on and off", runs["on"] == runs["off"]),
        ("metrics were emitted when a backend was installed", len(recorded) > 0),
        ("metric attributes stay within the closed set", keys <= commerce_metrics.ALLOWED_ATTRIBUTES),
    ])


@scenario("S13", "commerce disabled with one outcome_unknown invocation outstanding")
def s13(env: Env) -> Result:
    with env.new_world() as w:
        async def slow() -> dict[str, Any]:
            g = await w.gateway()
            w.gate = asyncio.Event()
            first = await g.invoke(_cap(), {"ticket": "OPS-1"})
            w.gate.set()
            await asyncio.sleep(0.05)  # the provider finishes; the ledger does not know yet
            return first

        first = _run(slow())
        phase_before = _rows(w)[0]["phase"]
        # Layer 2 (mctl-agents): no runtime, no terms read, so the call is
        # dispatched as legacy and the provider ingress fence refuses it.
        flag_off = _call(w, "OPS-2", gateway=_run(w.gateway(commerce=False)))
        # Layer 1 (mctl-api): the accept-only switch.
        w.ledger.accepting = False
        switched = _call(w, "OPS-3")
        reconciled = w.ledger.reconcile(lambda key: w.ingress.outcome(key).status)
        phase_after = _rows(w)[0]["phase"]
        budget = _budget(w)  # tenant reads keep working
        observed = {"first": first["reason_code"], "phase_before": phase_before, "flag_off": flag_off["reason_code"],
                    "switch_on": switched["reason_code"], "reconciled": reconciled, "phase_after": phase_after,
                    "committed": budget["committed_minor"], "reserved": budget["reserved_minor"],
                    "executions": len(w.executions), "invocations": len(_rows(w))}
        return _finish("S13", NAMES["S13"], {"new_call": "refused", "pending": "terminal"}, observed, [
            ("one invocation is outstanding", first["reason_code"] == "outcome-unknown"
             and phase_before == "outcome_unknown"),
            ("with the flag off the provider fence refuses the unsigned dispatch",
             flag_off["reason_code"] == "provider-error"),
            ("with the accept switch on a new call is refused unsupported-execution",
             switched["reason_code"] == "unsupported-execution"),
            ("no new invocation was opened", observed["invocations"] == 1),
            ("the reconciler resolves the outstanding invocation", reconciled == 1 and phase_after in TERMINAL),
            ("the tenant reads keep working and nothing is stranded", budget["reserved_minor"] == 0
             and budget["committed_minor"] == PRICE_MINOR),
            ("the provider ran once", observed["executions"] == 1),
        ])


@scenario("S14", "end-of-run liability sweep")
def s14(env: Env) -> Result:
    """A complement test, not an enumeration: every invocation this run
    created is read unfiltered (since the earliest world opened) and any
    phase outside `TERMINAL`, known or not, is outstanding. Tenant-wide reads
    of each known non-terminal phase cross-check it, so a stale row from an
    earlier run is caught too. Any read that fills a page fails rather than
    passing on a partial list. `invocations_swept` counts the rows read
    since the earliest world's `proof_since` (rows from the lookback window
    before it included, so a stale one is caught) plus the cross-check rows.
    Every invocation any closed world created must be in that scoped
    read, so a sweep that silently missed the run's rows cannot pass; a run
    whose scenarios created none (S6 alone, or a live rerun where every
    budget-bound scenario skipped) and a clean tenant pass. With no world
    opened yet (`--only S14`) it opens one, so the reads always run."""
    if not env.worlds:
        with env.new_world():
            pass
    groups: dict[tuple[int, str], list[Any]] = {}
    for w in env.worlds:
        groups.setdefault((id(w.ledger), w.tenant), []).append(w)  # live worlds share one client and tenant
    outstanding: dict[str, dict[str, str]] = {}
    swept = reads = 0
    scoped_ids: set[str] = set()

    def flag(row: Mapping[str, Any]) -> None:
        outstanding.setdefault(str(row["invocation_id"]), {
            "invocation_id": str(row["invocation_id"]), "phase": str(row.get("phase")),
            "reason_code": row.get("reason_code", "") or "unexplained"})

    for worlds in groups.values():
        w = worlds[0]
        since = min(x.proof_since for x in worlds)
        rows = _list(w, since=since)
        reads += 1
        swept += len(rows)
        scoped_ids.update(str(r["invocation_id"]) for r in rows)
        for row in rows:
            if row.get("phase") not in TERMINAL:
                flag(row)
        for phase in NON_TERMINAL:
            phase_rows = _list(w, phase=phase)
            reads += 1
            swept += len(phase_rows)
            for row in phase_rows:
                flag(row)
    missed = sorted(env.created - scoped_ids)
    observed = {"ledgers": len(groups), "reads": reads, "invocations_swept": swept,
                "created": len(env.created), "created_unreadable": list(env.created_unreadable),
                "missed": missed, "non_terminal": list(outstanding.values())}
    return _finish("S14", NAMES["S14"], {"non_terminal": 0}, observed, [
        ("every world's invocations were read when it closed", not env.created_unreadable),
        ("every invocation this run created was swept", not missed),
        ("zero invocations outside a terminal phase", not outstanding),
        ("every outstanding one carries a reason",
         all(o["reason_code"] != "unexplained" for o in outstanding.values())),
    ])


# ---------------------------------------------------------------------------
# Suite, bundle, report
# ---------------------------------------------------------------------------

ORDER = tuple(f"S{i}" for i in range(1, 15))


def run_suite(env: Env, only: list[str] | None = None) -> list[Result]:
    """S14 always runs last, over every world the other scenarios opened."""
    results: list[Result] = []
    for sid in env.order or ORDER:
        if only and sid not in only:
            continue
        if sid in env.not_driven:
            note = env.not_driven[sid]
            results.append(Result(sid, NAMES[sid], OFFLINE_ONLY if note == OFFLINE_ONLY_SETTLEMENT else SKIPPED,
                                  {}, {}, [], note=note))
            continue
        try:
            results.append(SCENARIOS[sid](env))
        except Skip as exc:
            results.append(Result(sid, NAMES[sid], SKIPPED, {}, {}, [], note=str(exc)))
        except Exception as exc:  # noqa: BLE001 — an unobservable scenario fails, never passes
            results.append(Result(sid, NAMES[sid], FAILED, {}, {"error": type(exc).__name__}, [],
                                  note=f"{type(exc).__name__}: {exc}"))
    return results


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,  # noqa: S603, S607
                              check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def build_bundle(
    results: list[Result], *, backend: str, terms: Mapping[str, Any], mctl_agents_sha: str,
    mctl_api_sha: str = "not-recorded", image_tags: Mapping[str, str] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """The machine-readable evidence. Pure. No arguments, results, tokens or
    payment proofs."""
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION, "issue": "mctlhq/mctl-agents#620", "backend": backend,
        "generated_at": generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mctl_agents_sha": mctl_agents_sha, "mctl_api_sha": mctl_api_sha, "image_tags": dict(image_tags or {}),
        "provider_contract": {
            "dispatch": cm.DISPATCH_AUTHORIZATION_VERSION, "quote": cm.QUOTE_ENCODING_VERSION,
            "terms": cm.TERMS_ENCODING_VERSION,
        },
        "terms": {"terms_id": terms.get("terms_id", ""), "terms_version": terms.get("terms_version", 0)},
        "config": {"price_mode": "per_successful_call", "price": {"currency": "EUR", "amount_minor": PRICE_MINOR},
                   "budget_minor": BUDGET_MINOR, "funds_mode": "simulated", "provider_id": cp.PILOT_PROVIDER_ID},
        "scenarios": [r.to_dict() for r in results],
        "summary": {"counts": counts, "failed": [r.id for r in results if r.status == FAILED],
                    # Never read as passed: each with the reason it was not driven here.
                    "not_driven": {r.id: r.note for r in results if r.status in (SKIPPED, OFFLINE_ONLY)},
                    "ok": not any(r.status == FAILED for r in results)},
    }


def render_markdown(bundle: Mapping[str, Any]) -> str:
    lines = [
        "| Scenario | Result | Expected | Observed |", "| --- | --- | --- | --- |",
    ]
    for s in bundle["scenarios"]:
        lines.append(f"| {s['id']} {s['name']} | {s['status']} | `{json.dumps(s['expected'], sort_keys=True)}` "
                     f"| `{json.dumps(s['observed'], sort_keys=True)}` |")
    lines += ["", f"Backend: {bundle['backend']}. mctl-agents {bundle['mctl_agents_sha']}, "
                  f"mctl-api {bundle['mctl_api_sha']}.",
              f"Contract: {', '.join(bundle['provider_contract'].values())}. "
              f"Terms {bundle['terms']['terms_id']} v{bundle['terms']['terms_version']}."]
    return "\n".join(lines)


def write_bundle(bundle: Mapping[str, Any], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _terms_of(env: Env) -> Mapping[str, Any]:
    with env.new_world() as w:
        answer = w.ledger.current_terms(_cap(), w.schema_hash())
        return dict(answer.body) if answer.ok else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=("offline", "live"), default="offline")
    parser.add_argument("--out", help="write the evidence bundle to this path (required with --backend live)")
    parser.add_argument("--only", nargs="*", help="scenario ids, e.g. S1 S4")
    parser.add_argument("--mctl-api-sha", default=os.environ.get("MCTL_API_SHA", "not-recorded"))
    parser.add_argument("--image-tag", action="append", default=[], metavar="NAME=TAG")
    parser.add_argument("--markdown", action="store_true", help="print the summary table")
    args = parser.parse_args(argv)
    if args.backend == "live" and not args.out:
        parser.error("--backend live needs --out: a live run without its evidence bundle proves nothing")
    bad_tags = [item for item in args.image_tag if "=" not in item or not item.split("=", 1)[1]]
    if bad_tags:
        parser.error(f"--image-tag takes NAME=TAG, got {bad_tags!r}")

    try:
        env = offline_env() if args.backend == "offline" else live_env()
        results = run_suite(env, args.only)
        # S14 sweeps the worlds the others opened, so it runs after them above.
        bundle = build_bundle(
            results, backend=args.backend, terms=_terms_of(env), mctl_agents_sha=_git("rev-parse", "HEAD"),
            mctl_api_sha=args.mctl_api_sha,
            image_tags=dict(item.split("=", 1) for item in args.image_tag),
        )
    except ProofError as exc:
        print(f"commerce_proof: {exc}", file=sys.stderr)
        return 2
    if args.out:
        write_bundle(bundle, Path(args.out))
    print(render_markdown(bundle) if args.markdown else json.dumps(bundle["summary"], sort_keys=True))
    return 0 if bundle["summary"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
