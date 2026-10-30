# ruff: noqa: E501
"""A fake mctl-api SecurityFinding store implementing the 4.124.0 transition
table in `docs/security-findings.md`, plugged in as the client transport.

It is deliberately small: only what the runner's routes need, plus `ingest`
(the provider-only observation route, which the runner never calls) so
scenario tests can play the provider's part.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

from orchestrator.security_findings.client import HTTPResult, SecurityFindingsClient, SecurityUnavailable

STRENGTHS = ["deterministic_regression_test", "assurance_replay", "scanner_rerun_exact_revision", "provider_claim", "manual"]
MIN_STRENGTH = "scanner_rerun_exact_revision"


def rank(s: str) -> int:
    return STRENGTHS.index(s)


class FakeStore:
    def __init__(self, *, tenant: str = "admins") -> None:
        self.tenant = tenant
        self.occ: dict[str, dict[str, Any]] = {}
        self.findings: dict[str, dict[str, Any]] = {}
        self.dev_loops: dict[str, str] = {}
        self.requests: list[tuple[str, str, dict[str, Any] | None, dict[str, str]]] = []
        self.idem: dict[str, tuple[str, dict[str, Any]]] = {}
        self.down = False
        self.transport_error = False
        self.forbid_writes = False
        self.dev_loop_status = 200
        self.n = 0
        self.clock = "2026-10-03T00:00:00Z"

    # -- seeding ------------------------------------------------------------

    def seed(
        self, oid: str = "soc_1", *, finding: str = "sf_1", repo: str = "github.com/mctlhq/svc",
        identity: str = "cve:CVE-2026-1", state: str = "open", untrusted: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.findings.setdefault(finding, {"id": finding, "identity_key": identity})
        o = {
            "id": oid, "finding_id": finding, "tenant": self.tenant, "state": state, "state_version": 1,
            "subject": {"repository": repo, "package_purl": "pkg:golang/x@1"},
            "remediation_attempts": [], "verifications": [], "observations": [],
            "untrusted": untrusted or {},
        }
        self.occ[oid] = o
        return o

    def install(self, monkeypatch: Any) -> None:
        def request(self_: SecurityFindingsClient, method: str, path: str, payload: dict[str, Any] | None = None,
                    headers: dict[str, str] | None = None) -> HTTPResult:
            return self.handle(method, path, payload, headers or {})

        monkeypatch.setattr(SecurityFindingsClient, "_request", request)

    # -- helpers --------------------------------------------------------------

    def _id(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}_{self.n}"

    def _bump(self, o: dict[str, Any], state: str | None = None) -> None:
        if state:
            o["state"] = state
        o["state_version"] += 1

    @staticmethod
    def _err(status: int, code: str) -> HTTPResult:
        return HTTPResult(status, {"error": code, "code": code})

    def active(self, o: dict[str, Any]) -> dict[str, Any] | None:
        return next((a for a in o["remediation_attempts"] if "outcome" not in a), None)

    def _view(self, o: dict[str, Any]) -> dict[str, Any]:
        a = self.active(o)
        return {**json.loads(json.dumps(o)), "active_attempt_id": a["id"] if a else ""}

    # -- transport --------------------------------------------------------------

    def handle(self, method: str, path: str, payload: dict[str, Any] | None, headers: dict[str, str]) -> HTTPResult:
        self.requests.append((method, path, payload, headers))
        if self.transport_error:
            raise SecurityUnavailable("connection refused")
        url = urlparse(path)
        parts = url.path.strip("/").split("/")
        if parts[:3] == ["api", "v1", "agents"]:
            wid = parts[4]
            if wid in self.dev_loops:
                return HTTPResult(self.dev_loop_status, {"status": self.dev_loops[wid]})
            return HTTPResult(404 if self.dev_loop_status == 200 else self.dev_loop_status, {})
        if self.down:
            return self._err(503, "security_findings_store_unavailable")
        if method == "POST" and self.forbid_writes:
            return self._err(403, "forbidden")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if method == "GET":
            return self._get(parts, q)
        return self._post(parts, payload or {}, headers)

    def _get(self, parts: list[str], q: dict[str, str]) -> HTTPResult:
        if parts[3:] == ["occurrences"]:
            rows = [self._view(o) for o in self.occ.values() if not q.get("state") or o["state"] == q["state"]]
            for r in rows:  # summaries omit the history arrays
                for k in ("remediation_attempts", "verifications", "observations"):
                    r.pop(k)
            return HTTPResult(200, {"occurrences": rows})
        if parts[3] == "occurrences":
            o = self.occ.get(parts[4])
            return HTTPResult(200, self._view(o)) if o else self._err(404, "not_found")
        if parts[3] == "findings":
            f = self.findings.get(parts[4])
            return HTTPResult(200, f) if f else self._err(404, "not_found")
        return self._err(404, "not_found")

    def _post(self, parts: list[str], body: dict[str, Any], headers: dict[str, str]) -> HTTPResult:
        key = headers.get("Idempotency-Key", "")
        tag = json.dumps([parts, body], sort_keys=True)
        if key in self.idem:
            old_tag, old = self.idem[key]
            if old_tag != tag:
                return self._err(409, "idempotency_key_reused")
            return HTTPResult(200, {**old, "replayed": True})
        res = self._apply(parts, body)
        if 200 <= res.status < 300 and key:
            self.idem[key] = (tag, res.payload)
        return res

    def _apply(self, parts: list[str], body: dict[str, Any]) -> HTTPResult:
        if parts[3] == "occurrences" and parts[5] == "remediation-attempts":
            o = self.occ.get(parts[4])
            if o is None:
                return self._err(404, "not_found")
            if body.get("expected_state_version") not in (None, o["state_version"]):
                return self._err(409, "version_conflict")
            if self.active(o) is not None and o["state"] != "unverifiable":
                return self._err(409, "remediation_attempt_active")
            if o["state"] not in ("open", "triaged", "reopened", "unverifiable"):
                return self._err(409, "invalid_transition")
            if o["state"] == "unverifiable" and (prev := self.active(o)):
                prev["outcome"] = "superseded"
            a = {"id": self._id("sra"), "occurrence_id": o["id"], "seq": len(o["remediation_attempts"]) + 1,
                 "work_item_id": body["work_item_id"], "devloop_workflow_id": body.get("devloop_workflow_id", ""),
                 "start_source": body.get("start_source", "")}
            o["remediation_attempts"].append(a)
            self._bump(o, "fixing")
            return HTTPResult(201, self._result(o, a, "start_remediation"))
        if parts[3] == "remediation-attempts":
            o, a = self._find_attempt(parts[4])
            if o is None or a is None:
                return self._err(404, "not_found")
            if body.get("expected_state_version") not in (None, o["state_version"]):
                return self._err(409, "version_conflict")
            if parts[5] == "fix":
                if o["state"] != "fixing" or "outcome" in a:
                    return self._err(409, "invalid_transition")
                if not body.get("landed_revision") and not body.get("landed_artifact_digest"):
                    return self._err(400, "invalid_request")
                a["landed_revision"] = body.get("landed_revision", "")
                a["landed_ref"] = body.get("landed_ref", "")
                a["landed_at"] = "2026-10-02T00:00:00Z"
                self._bump(o, "awaiting_verification")
                return HTTPResult(200, self._result(o, a, "record_fix"))
            if parts[5] == "verifications":
                return self._verify(o, a, body)
        return self._err(404, "not_found")

    def _find_attempt(self, aid: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        for o in self.occ.values():
            for a in o["remediation_attempts"]:
                if a["id"] == aid:
                    return o, a
        return None, None

    def _result(self, o: dict[str, Any], a: dict[str, Any] | None, transition: str, **extra: Any) -> dict[str, Any]:
        return {"occurrence_id": o["id"], "attempt_id": a["id"] if a else "", "state": o["state"],
                "state_version": o["state_version"], "transition": transition, "replayed": False, **extra}

    def _verify(self, o: dict[str, Any], a: dict[str, Any], body: dict[str, Any]) -> HTTPResult:
        if o["state"] != "awaiting_verification" or "outcome" in a:
            return self._err(409, "invalid_transition")
        if body.get("revision", a.get("landed_revision", "")) != a.get("landed_revision", ""):
            return self._err(422, "verification_not_bound")
        mech, result = body["mechanism"], body["result"]
        derived = max(rank(mech), rank("deterministic_regression_test"))
        if mech == "assurance_replay" and not any(e.get("kind") == "assurance_finding" for e in body.get("evidence_refs", [])):
            derived = max(derived, rank("provider_claim"))
        return self._record(o, a, mech, result, derived, body.get("revision", ""), "agent:security", body.get("evidence_refs", []))

    def _record(self, o: dict[str, Any], a: dict[str, Any], mech: str, result: str, derived: int, revision: str,
                by: str, evidence: list[dict[str, Any]] | None = None) -> HTTPResult:
        v = {"id": self._id("svr"), "attempt_id": a["id"], "occurrence_id": o["id"], "mechanism": mech,
             "derived_strength": STRENGTHS[derived], "result": result, "revision": revision, "recorded_by": by,
             "recorded_at": self.clock, "evidence_refs": evidence or []}
        o["verifications"].append(v)
        if result == "passed" and derived <= rank(MIN_STRENGTH):
            a["outcome"] = "resolved"
            o["resolved_at_seq"] = len(o["verifications"])
            self._bump(o, "resolved")
            transition = "verify_passed"
        elif result == "failed":
            self._bump(o, "fixing")
            transition = "verify_failed"
        elif result == "unverifiable":
            self._bump(o, "unverifiable")
            transition = "verify_unverifiable"
        else:
            self._bump(o)
            transition = "verify_inconclusive"
        return HTTPResult(201, self._result(o, a, transition, verification_id=v["id"]))

    # -- the provider's part (never called by the runner) ---------------------------

    def ingest(self, oid: str, *, provider: str, ceiling: str = "scanner_rerun_exact_revision", action: str,
               revision: str = "", delivery: str = "d1", at: str = "2026-10-04T00:00:00Z",
               evidence: list[dict[str, str]] | None = None) -> str:
        if provider.startswith("admin:"):
            ceiling = "provider_claim"
        o = self.occ[oid]
        h = hashlib.sha256(json.dumps([provider, oid, action, revision, delivery], sort_keys=True).encode()).hexdigest()
        if any(x["observation_hash"] == h for x in o["observations"]):
            return "duplicate"
        ob = {"id": self._id("sob"), "provider_ref": provider, "observation_hash": h, "claimed_action": action,
              "observed_revision": revision, "observed_at": at, "evidence_refs": evidence or []}
        o["observations"].append(ob)
        a = self.active(o)
        if action == "present" and o["state"] == "resolved":
            fixed_affected = {x.get("landed_revision") for x in o["remediation_attempts"]}
            if revision not in fixed_affected and not revision.startswith("pre-"):
                self._bump(o, "reopened")
                return "reopened"
        if action == "absent" and o["state"] == "awaiting_verification" and a and revision == a.get("landed_revision"):
            self._record(o, a, "scanner_rerun_exact_revision", "passed",
                         max(rank("scanner_rerun_exact_revision"), rank(ceiling)), revision, f"provider:{provider}")
            return "verified"
        if action == "fixed" and o["state"] == "awaiting_verification" and a and revision == a.get("landed_revision"):
            self._record(o, a, "provider_claim", "passed", rank("provider_claim"), revision, f"provider:{provider}")
            return "provider_claim"
        return "recorded"

    def writes(self) -> list[tuple[str, str, dict[str, Any] | None, dict[str, str]]]:
        return [r for r in self.requests if r[0] == "POST"]


class FakeWorkItems:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def create_work_item(self, tenant: str, issue_url: str, run: str) -> Any:
        from orchestrator.work_context import creation

        self.calls.append((tenant, issue_url, run))
        return creation.CreateAnswer(creation.CREATED, work_item_id="wi_1")
