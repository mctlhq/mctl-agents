"""HTTP client for mctl-api's SecurityFinding v1 surface (mctl-api 4.124.0).

Transport is `orchestrator/work_context/client.py`'s: a synchronous `urllib`
client with the run-token bearer (`run_token.mctl_api_bearer`), https-only,
no redirects. Same contract too: uncertainty is a value, never an exception.
Every answer is `ok`, `refused(code)` or `unknown`:

* a 503, a transport error, a malformed body, or a code outside the fixture
  vocabulary is `unknown`, never `ok`, and never "no occurrence";
* a 4xx with a known code is `refused(code)`;
* a 403 on a write latches the client: no further write is sent.

Only the three mutation routes this slice needs exist here. Triage, dismiss
and observation ingest are deliberately absent (dismiss is human-only, ingest
is provider-only). A test pins `ROUTES` as a subset of `routes.json`.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from orchestrator.security_findings import contract

DEFAULT_TIMEOUT_S = 10
LIST_PAGE_LIMIT = 200
LIST_MAX_PAGES = 10

OK = "ok"
REFUSED = "refused"
UNKNOWN = "unknown"

#: Every key is a `"METHOD path"` entry of tests/fixtures/security_finding_v1/routes.json.
ROUTES = {
    "list_findings": "/api/v1/security/findings",
    "get_finding": "/api/v1/security/findings/{id}",
    "list_occurrences": "/api/v1/security/occurrences",
    "get_occurrence": "/api/v1/security/occurrences/{id}",
    "start_remediation": "/api/v1/security/occurrences/{id}/remediation-attempts",
    "record_fix": "/api/v1/security/remediation-attempts/{id}/fix",
    "record_verification": "/api/v1/security/remediation-attempts/{id}/verifications",
}
ROUTE_METHODS = {
    "list_findings": "GET",
    "get_finding": "GET",
    "list_occurrences": "GET",
    "get_occurrence": "GET",
    "start_remediation": "POST",
    "record_fix": "POST",
    "record_verification": "POST",
}
MUTATIONS = frozenset({"start_remediation", "record_fix", "record_verification"})

#: Not a security route: the start guard's DevLoop existence read.
DEV_LOOP_ROUTE = "/api/v1/agents/dev-loop/{id}"

START_SOURCE = "agent:security-remediation"


def api_base() -> str:
    return os.environ.get("MCTL_API_BASE_URL", "https://api.mctl.ai").rstrip("/")


def idempotency_key(name: str, *parts: object) -> str:
    """`<name>:<sha256>` over the immutable inputs, so a retry replays."""
    digest = hashlib.sha256("\x1f".join([name, *map(str, parts)]).encode()).hexdigest()
    return f"{name}:{digest}"


class SecurityUnavailable(RuntimeError):
    """Raised only by the low-level transport; public methods turn it into `unknown`."""


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    class _NoRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    return urllib.request.build_opener(_NoRedirects)


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _q(value: str) -> str:
    return quote(str(value), safe="")


@dataclass(frozen=True)
class HTTPResult:
    status: int
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OccurrenceAnswer:
    verdict: str
    occurrence: contract.Occurrence | None = None
    code: str = ""
    reason: str = ""


@dataclass(frozen=True)
class OccurrencesAnswer:
    """A complete listing, or `unknown`: never a truncated list."""

    verdict: str
    occurrences: tuple[contract.Occurrence, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class FindingAnswer:
    verdict: str
    identity_key: str = ""
    code: str = ""


@dataclass(frozen=True)
class MutationAnswer:
    verdict: str
    code: str = ""
    reason: str = ""
    occurrence_id: str = ""
    attempt_id: str = ""
    verification_id: str = ""
    state: str = ""
    state_version: int = 0
    transition: str = ""
    replayed: bool = False

    @property
    def ok(self) -> bool:
        return self.verdict == OK


@dataclass(frozen=True)
class DevLoopAnswer:
    """`absent` (404), `exists` (200, with its status) or `unknown`."""

    verdict: str
    status: str = ""
    reason: str = ""


def classify_error(status: int, payload: dict[str, Any]) -> tuple[str, str]:
    """`(verdict, code)` of a non-2xx answer."""
    code = payload.get("code")
    code = code if isinstance(code, str) else ""
    if status == 403:
        return REFUSED, "forbidden"
    if status >= 500 or status in (401, 408, 429):
        return UNKNOWN, code if code in contract.ERROR_CODES else ""
    if code in contract.UNAVAILABLE_CODES or code not in contract.ERROR_CODES:
        return UNKNOWN, ""
    return REFUSED, code


def bearer_kind() -> str:
    """`run_token` in enforce mode, else `service` (the static MCTL_TOKEN)."""
    from orchestrator import run_token

    try:
        return "run_token" if run_token.mode() == run_token.MODE_ENFORCE else "service"
    except run_token.RunTokenError:
        return "run_token"


def audit(event: str, **fields: object) -> None:
    """One structured line per write (stderr, so stdout stays the CLI's output)."""
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=sys.stderr, flush=True)


class SecurityFindingsClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: int = DEFAULT_TIMEOUT_S) -> None:
        self._base = (base_url or api_base()).rstrip("/")
        self._token = token
        self._timeout = timeout
        self._opener = _no_redirect_opener()
        #: Set by a 403 on a write: no further write is sent by this client.
        self.forbidden_route = ""

    # -- transport --------------------------------------------------------

    def _bearer(self) -> str:
        from orchestrator import run_token

        return run_token.mctl_api_bearer(self._token)

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> HTTPResult:
        token = self._bearer()
        if not token:
            raise SecurityUnavailable("no mctl-api credential: MCTL_TOKEN is not set, or enforce mode has no run token")
        url = f"{self._base}{path}"
        if not url.startswith("https://"):
            raise SecurityUnavailable(f"refusing non-https MCTL_API_BASE_URL: {self._base}")
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 — scheme checked above
        req.add_header("Authorization", f"Bearer {token}")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                body = resp.read()
                status = resp.getcode() or 200
        except urllib.error.HTTPError as exc:
            try:
                parsed: Any = json.loads(exc.read() or b"{}")
            except Exception:  # noqa: BLE001 — an unreadable error body is still just an error
                parsed = {}
            return HTTPResult(exc.code, parsed if isinstance(parsed, dict) else {})
        except Exception as exc:
            raise SecurityUnavailable(str(exc)) from exc
        try:
            parsed = json.loads(body or b"{}")
        except ValueError as exc:
            raise SecurityUnavailable(f"malformed response: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SecurityUnavailable("malformed response: not an object")
        return HTTPResult(status, parsed)

    # -- reads ------------------------------------------------------------

    def get_occurrence(self, occurrence_id: str) -> OccurrenceAnswer:
        path = ROUTES["get_occurrence"].format(id=_q(occurrence_id))
        try:
            res = self._request("GET", path)
        except SecurityUnavailable as exc:
            return OccurrenceAnswer(UNKNOWN, reason=str(exc))
        if 200 <= res.status < 300:
            occ = contract.parse_occurrence(res.payload)
            if occ is None or occ.id != occurrence_id:
                return OccurrenceAnswer(UNKNOWN, reason="malformed occurrence body")
            return OccurrenceAnswer(OK, occ)
        verdict, code = classify_error(res.status, res.payload)
        return OccurrenceAnswer(verdict, code=code, reason=f"HTTP {res.status}")

    def get_finding_identity(self, finding_id: str) -> FindingAnswer:
        try:
            res = self._request("GET", ROUTES["get_finding"].format(id=_q(finding_id)))
        except SecurityUnavailable:
            return FindingAnswer(UNKNOWN)
        if 200 <= res.status < 300:
            key = contract.parse_finding_identity(res.payload)
            return FindingAnswer(OK, identity_key=key) if key else FindingAnswer(UNKNOWN)
        verdict, code = classify_error(res.status, res.payload)
        return FindingAnswer(verdict, code=code)

    def list_occurrences(
        self, *, state: str = "", repository: str = "", tenant: str = "", max_items: int = 0
    ) -> OccurrencesAnswer:
        """Every summary matching the filters, or `unknown` when any page fails
        or the listing is longer than `LIST_MAX_PAGES` pages. `max_items` > 0
        stops (successfully) once that many are collected."""
        collected: list[contract.Occurrence] = []
        cursor = ""
        for _ in range(LIST_MAX_PAGES):
            query = {"limit": str(LIST_PAGE_LIMIT)}
            for key, value in (("state", state), ("repository", repository), ("tenant", tenant), ("cursor", cursor)):
                if value:
                    query[key] = value
            path = f"{ROUTES['list_occurrences']}?{urlencode(query)}"
            try:
                res = self._request("GET", path)
            except SecurityUnavailable as exc:
                return OccurrencesAnswer(UNKNOWN, reason=str(exc))
            if not 200 <= res.status < 300 or not isinstance(res.payload.get("occurrences"), list):
                return OccurrencesAnswer(UNKNOWN, reason=f"HTTP {res.status}")
            for raw in res.payload["occurrences"]:
                occ = contract.parse_occurrence(raw)
                if occ is None:
                    return OccurrencesAnswer(UNKNOWN, reason="malformed occurrence in listing")
                collected.append(occ)
            nxt = res.payload.get("next_cursor")
            if max_items and len(collected) >= max_items:
                return OccurrencesAnswer(OK, tuple(collected[:max_items]))
            if not isinstance(nxt, str) or not nxt:
                return OccurrencesAnswer(OK, tuple(collected))
            cursor = nxt
        return OccurrencesAnswer(UNKNOWN, reason=f"listing is longer than {LIST_MAX_PAGES} pages")

    def get_dev_loop(self, workflow_id: str) -> DevLoopAnswer:
        """404 is `absent`, 200 is `exists`; everything else is `unknown`."""
        try:
            res = self._request("GET", DEV_LOOP_ROUTE.format(id=_q(workflow_id)))
        except SecurityUnavailable as exc:
            return DevLoopAnswer(UNKNOWN, reason=str(exc))
        if res.status == 404:
            return DevLoopAnswer("absent")
        if res.status == 200:
            status = res.payload.get("status")
            return DevLoopAnswer("exists", status=status if isinstance(status, str) else "")
        return DevLoopAnswer(UNKNOWN, reason=f"HTTP {res.status}")

    # -- writes -----------------------------------------------------------

    def _write(
        self,
        route: str,
        target_id: str,
        body: dict[str, Any],
        key: str,
        expected_state_version: int,
        **audit_fields: object,
    ) -> MutationAnswer:
        if route not in MUTATIONS:
            raise ValueError(f"not a mutation route: {route}")
        if self.forbidden_route:
            return MutationAnswer(
                REFUSED, code="forbidden", reason=f"earlier 403 on {self.forbidden_route}; no further writes"
            )
        body = {**body, "expected_state_version": expected_state_version}
        path = ROUTES[route].format(id=_q(target_id))
        try:
            res = self._request(ROUTE_METHODS[route], path, body, {"Idempotency-Key": key})
        except SecurityUnavailable as exc:
            answer = MutationAnswer(UNKNOWN, reason=str(exc))
        else:
            answer = self._mutation_answer(route, res)
        if answer.code == "forbidden" and answer.verdict == REFUSED:
            self.forbidden_route = f"{ROUTE_METHODS[route]} {ROUTES[route]}"
            audit("security-finding-forbidden", route=self.forbidden_route, bearer=bearer_kind())
        audit(
            "security-finding-write",
            route=route,
            target=target_id,
            idempotency_key=key,
            expected_state_version=expected_state_version,
            verdict=answer.verdict,
            code=answer.code,
            transition=answer.transition,
            replayed=answer.replayed,
            attempt_id=answer.attempt_id or audit_fields.get("attempt_id", ""),
            occurrence_id=answer.occurrence_id or audit_fields.get("occurrence_id", ""),
        )
        return answer

    @staticmethod
    def _mutation_answer(route: str, res: HTTPResult) -> MutationAnswer:
        if not 200 <= res.status < 300:
            verdict, code = classify_error(res.status, res.payload)
            return MutationAnswer(verdict, code=code, reason=f"HTTP {res.status}")
        p = res.payload
        version = p.get("state_version")
        if (
            not isinstance(p.get("occurrence_id"), str)
            or not isinstance(p.get("state"), str)
            or not isinstance(version, int)
            or isinstance(version, bool)
        ):
            return MutationAnswer(UNKNOWN, reason="malformed mutation result")
        state = p["state"]
        if state not in contract.STATES:
            return MutationAnswer(UNKNOWN, reason=f"state outside the 4.124.0 vocabulary: {state!r}")
        return MutationAnswer(
            OK,
            occurrence_id=p["occurrence_id"],
            attempt_id=_str(p.get("attempt_id")),
            verification_id=_str(p.get("verification_id")),
            state=state,
            state_version=version,
            transition=_str(p.get("transition")),
            replayed=p.get("replayed") is True,
        )

    def start_remediation(
        self,
        occurrence_id: str,
        *,
        work_item_id: str,
        devloop_workflow_id: str,
        expected_state_version: int,
        idempotency_key: str,
        start_source: str = START_SOURCE,
    ) -> MutationAnswer:
        body = {"work_item_id": work_item_id, "devloop_workflow_id": devloop_workflow_id, "start_source": start_source}
        return self._write(
            "start_remediation", occurrence_id, body, idempotency_key, expected_state_version,
            occurrence_id=occurrence_id,
        )

    def record_fix(
        self,
        attempt_id: str,
        *,
        landed_revision: str,
        landed_ref: str,
        expected_state_version: int,
        idempotency_key: str,
        occurrence_id: str = "",
    ) -> MutationAnswer:
        body = {"landed": True, "landed_revision": landed_revision, "landed_ref": landed_ref}
        return self._write(
            "record_fix", attempt_id, body, idempotency_key, expected_state_version,
            attempt_id=attempt_id, occurrence_id=occurrence_id,
        )

    def record_verification(
        self,
        attempt_id: str,
        *,
        mechanism: str,
        result: str,
        revision: str = "",
        artifact_digest: str = "",
        evidence_refs: list[dict[str, str]] | None = None,
        expected_state_version: int,
        idempotency_key: str,
        occurrence_id: str = "",
    ) -> MutationAnswer:
        """No strength field exists on this method: mctl-api derives it. The
        agent never records `provider_claim` or `manual`, or a result outside
        the vocabulary; those are refused locally and nothing is sent."""
        if mechanism not in contract.AGENT_MECHANISMS:
            return MutationAnswer(REFUSED, code="local_mechanism_not_allowed", reason=mechanism)
        if result not in contract.RESULTS:
            return MutationAnswer(REFUSED, code="local_result_unknown", reason=result)
        body: dict[str, Any] = {"mechanism": mechanism, "result": result}
        if revision:
            body["revision"] = revision
        if artifact_digest:
            body["artifact_digest"] = artifact_digest
        if evidence_refs:
            body["evidence_refs"] = evidence_refs
        return self._write(
            "record_verification", attempt_id, body, idempotency_key, expected_state_version,
            attempt_id=attempt_id, occurrence_id=occurrence_id,
        )
