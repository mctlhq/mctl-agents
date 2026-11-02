"""Version-bound independent review receipts (ADR 025, mctl-agents#905).

Tier A, pure: the request/receipt documents, the spike report digest, an
async reader that fills the digest's inventory from GitHub, and the review
gate. Tier B (private, durable, authenticated storage and ingestion) is
owned by mctl-api and is NOT implemented here; nothing in this module reads
or writes it, and nothing consumes the gate yet.

Two rules shape everything below:

- A receipt is valid only for the exact subject bytes and Git SHAs it was
  issued for. Any difference makes it ``stale``.
- Evidence that was not observed is never ``PASS``, and a decision that
  cannot be made because the live revision or the authoring execution is not
  observable is ``unknown``, never ``missing`` or ``satisfied``.

GitHub review state, ``commit_id`` and check status are deliberately not
inputs of ``evaluate_review_gate``, and this module never names a
``run_shepherd.GATING_BOTS`` login or imports a GitHub writer.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from orchestrator.approval_evidence import GRANT_REF_RE
from orchestrator.context_snapshot import canonical_json, hash_bytes
from orchestrator.execution_evidence import ArtifactRef
from orchestrator.proposal_state import content_digest_of

API_VERSION = "review.mctl.ai/v1alpha1"
REQUEST_KIND = "ReviewRequest"
RECEIPT_KIND = "ReviewReceipt"
REQUEST_ID_PREFIX = "rr_"
RECEIPT_ID_PREFIX = "rc_"

SUBJECT_SPIKE_REPORT = "spike_report"
SUBJECT_PROPOSAL = "proposal"
SUBJECT_KINDS = frozenset({SUBJECT_SPIKE_REPORT, SUBJECT_PROPOSAL})

SPIKE_DIGEST_VERSION = "spike_report_digest_v1"
MEASUREMENTS_DIR = "measurements"
MAX_SPIKE_FILES = 512
MAX_SPIKE_BYTES = 32 * 1024 * 1024
_REGULAR_MODES = frozenset({"100644", "100755"})

OUTCOMES = ("PASS", "CHANGES_REQUESTED", "NEEDS_HUMAN", "UNOBSERVABLE")
CRITERION_STATUSES = ("met", "unmet", "unobserved")
MODES = ("worker", "manual")
ASSURANCES = ("independent", "advisory")

RUBRIC_SPIKE = "spike_v1"
RUBRIC_PROPOSAL = "proposal_v1"
RUBRICS: Mapping[str, tuple[str, ...]] = {
    RUBRIC_SPIKE: (
        "hypothesis_stated",
        "method_described",
        "measurements_present",
        "measurements_reproducible",
        "claims_traceable_to_measurements",
        "limitations_stated",
    ),
    RUBRIC_PROPOSAL: (
        "scope_bounded",
        "contracts_specified",
        "acceptance_testable",
        "rollback_defined",
        "matches_issue",
    ),
}
RUBRIC_FOR_SUBJECT = {SUBJECT_SPIKE_REPORT: RUBRIC_SPIKE, SUBJECT_PROPOSAL: RUBRIC_PROPOSAL}

# Gate answers.
GATE_SATISFIED = "satisfied"
GATE_STALE = "stale"
GATE_MISSING = "missing"
GATE_UNKNOWN = "unknown"
GATE_ADVISORY_ONLY = "advisory_only"
GATE_NOT_INDEPENDENT = "not_independent"
GATE_CHANGES_REQUESTED = "changes_requested"
GATE_NEEDS_HUMAN = "needs_human"
GATE_UNOBSERVABLE = "unobservable"
_OUTCOME_TO_GATE = {
    "PASS": GATE_SATISFIED,
    "CHANGES_REQUESTED": GATE_CHANGES_REQUESTED,
    "NEEDS_HUMAN": GATE_NEEDS_HUMAN,
    "UNOBSERVABLE": GATE_UNOBSERVABLE,
}
REQUIRED_ASSURANCES = ("independent", "any")

# Typed refusal codes (Tier A validation; the mctl-api adapter adds its own,
# listed in ADR 025).
CODE_PASS_WITH_UNOBSERVED = "review_pass_with_unobserved"  # noqa: S105 - a refusal code
CODE_UNOBSERVED_OUTCOME = "review_unobserved_outcome"
CODE_RUBRIC_MISMATCH = "review_rubric_mismatch"
CODE_MALFORMED = "review_malformed"
CODE_UNKNOWN_KEY = "review_unknown_key"
CODE_BAD_SUBJECT = "review_bad_subject"
CODE_BAD_IDENTITY = "review_bad_identity"

MAX_STR = 256
MAX_ACTORS = 64
_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_SERVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_EXECUTION_ID_RE = re.compile(rf"(?:we_{_UUID}|we_[0-9a-f]{{16,64}}|ex-[0-9a-f]{{16}})")
_EVIDENCE_ID_RE = re.compile(r"ev-[0-9a-f]{16}")
_CREATED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z")


class ReviewError(ValueError):
    """Fail-closed validation failure carrying a typed ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class SpikeDigestError(ReviewError):
    """No digest can be produced: refusing beats hashing a partial inventory."""

    def __init__(self, message: str) -> None:
        super().__init__("spike_digest_refused", message)


# ---------------------------------------------------------------------------
# spike_report_digest_v1
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InventoryEntry:
    """One regular file of a spike report, by report-root-relative path."""

    path: str
    content: bytes
    mode: str = "100644"


def _check_path(path: str) -> bytes:
    if not isinstance(path, str) or not path:
        raise SpikeDigestError("empty or non-string path")
    if "\\" in path or "\x00" in path:
        raise SpikeDigestError(f"path {path!r} holds a backslash or NUL")
    if path.startswith("/"):
        raise SpikeDigestError(f"path {path!r} is absolute")
    if any(seg in ("", ".", "..") for seg in path.split("/")):
        raise SpikeDigestError(f"path {path!r} has an empty, '.' or '..' segment")
    try:
        return path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SpikeDigestError(f"path {path!r} is not valid UTF-8") from exc


def spike_report_digest(entries: Sequence[InventoryEntry], *, report_path: str) -> str:
    """``spike_report_digest_v1`` over a complete inventory.

    ``entries`` must be the report file at ``report_path`` plus every regular
    file under ``measurements/``, nothing else. Bytes hashed::

        spike_report_digest_v1\\n
        for each entry sorted by UTF-8 path bytes:
            <len(path)>\\n<path>\\n<len(bytes)>\\n<bytes>
        <count>\\n

    The length prefixes make the framing unambiguous, so moving bytes between
    two files changes the digest.
    """
    report_bytes = _check_path(report_path)
    if not report_path.endswith(".md"):
        raise SpikeDigestError(f"report_path {report_path!r} is not a .md file")
    if len(entries) > MAX_SPIKE_FILES:
        raise SpikeDigestError(f"{len(entries)} files exceed the {MAX_SPIKE_FILES} cap")
    framed: dict[bytes, bytes] = {}
    folded: set[str] = set()
    total = 0
    for entry in entries:
        path_bytes = _check_path(entry.path)
        if entry.mode not in _REGULAR_MODES:
            raise SpikeDigestError(f"{entry.path!r} has non-regular mode {entry.mode!r}")
        if not isinstance(entry.content, bytes):
            raise SpikeDigestError(f"{entry.path!r} has no byte content")
        if entry.path != report_path and not entry.path.startswith(MEASUREMENTS_DIR + "/"):
            raise SpikeDigestError(f"{entry.path!r} is outside the report and {MEASUREMENTS_DIR}/")
        key = entry.path.casefold()
        if path_bytes in framed or key in folded:
            raise SpikeDigestError(f"{entry.path!r} duplicates or case-collides with another path")
        folded.add(key)
        total += len(entry.content)
        if total > MAX_SPIKE_BYTES:
            raise SpikeDigestError(f"inventory exceeds the {MAX_SPIKE_BYTES} byte cap")
        framed[path_bytes] = (
            f"{len(path_bytes)}\n".encode() + path_bytes + f"\n{len(entry.content)}\n".encode() + entry.content
        )
    if report_bytes not in framed:
        raise SpikeDigestError(f"report file {report_path!r} is not in the inventory")
    digest = hashlib.sha256()
    digest.update(f"{SPIKE_DIGEST_VERSION}\n".encode())
    for key_bytes in sorted(framed):
        digest.update(framed[key_bytes])
    digest.update(f"{len(framed)}\n".encode())
    return "sha256:" + digest.hexdigest()


def _git_blob_sha(sha: str, data: bytes) -> str:
    algo = hashlib.sha256 if len(sha) == 64 else hashlib.sha1
    return algo(b"blob %d\x00" % len(data) + data).hexdigest()


async def _get_json(client: httpx.AsyncClient, url: str, headers: Mapping[str, str]) -> dict[str, Any]:
    try:
        response = await client.get(url, headers=dict(headers))
    except httpx.RequestError as exc:
        raise SpikeDigestError(f"reading {url} failed: {exc}") from exc
    if response.status_code != 200:
        raise SpikeDigestError(f"reading {url} returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise SpikeDigestError(f"reading {url}: malformed response") from exc
    if not isinstance(body, dict):
        raise SpikeDigestError(f"reading {url}: malformed response")
    return body


async def read_spike_inventory(
    repository: str,
    report_root: str,
    report_path: str,
    sha: str,
    *,
    token: str,
    client: httpx.AsyncClient,
) -> list[InventoryEntry]:
    """The complete inventory of a spike report at one full commit SHA.

    ``report_path`` is relative to ``report_root``. Walks the git trees API
    down to the report root, lists it recursively, refuses a truncated
    listing, and verifies the git blob sha of every byte it returns. Any
    non-200, symlink, submodule, oversize or non-inlined blob raises
    ``SpikeDigestError``; a partial inventory is never returned.
    """
    if not _REPO_RE.fullmatch(repository):
        raise SpikeDigestError(f"bad repository {repository!r}")
    if not _SHA_RE.fullmatch(sha):
        raise SpikeDigestError("sha must be a full lowercase commit SHA")
    _check_path(report_root)
    _check_path(report_path)
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}
    base = f"https://api.github.com/repos/{repository}/git"
    tree_sha = sha
    for segment in report_root.split("/"):
        listing = await _get_json(client, f"{base}/trees/{tree_sha}", headers)
        if listing.get("truncated"):
            raise SpikeDigestError("truncated tree listing")
        found = [e for e in listing.get("tree", []) if isinstance(e, dict) and e.get("path") == segment]
        if len(found) != 1 or found[0].get("type") != "tree" or not isinstance(found[0].get("sha"), str):
            raise SpikeDigestError(f"{segment!r} is not a directory of {report_root!r} at {sha}")
        tree_sha = found[0]["sha"]
    listing = await _get_json(client, f"{base}/trees/{tree_sha}?recursive=1", headers)
    if listing.get("truncated") or not isinstance(listing.get("tree"), list):
        raise SpikeDigestError("truncated or malformed tree listing")
    wanted: list[dict[str, Any]] = []
    for node in listing["tree"]:
        if not isinstance(node, dict) or not isinstance(node.get("path"), str):
            raise SpikeDigestError("malformed tree entry")
        path = node["path"]
        if path != report_path and not (path == MEASUREMENTS_DIR or path.startswith(MEASUREMENTS_DIR + "/")):
            continue
        if node.get("type") == "tree" and path.startswith(MEASUREMENTS_DIR):
            continue
        if node.get("type") != "blob" or node.get("mode") not in _REGULAR_MODES:
            raise SpikeDigestError(
                f"{path!r} is not a regular file (type {node.get('type')!r}, mode {node.get('mode')!r})"
            )
        wanted.append(node)
    if len(wanted) > MAX_SPIKE_FILES:
        raise SpikeDigestError(f"{len(wanted)} files exceed the {MAX_SPIKE_FILES} cap")
    declared = 0
    for node in wanted:
        size = node.get("size")
        if not isinstance(size, int) or isinstance(size, bool):
            raise SpikeDigestError(f"{node['path']!r} has no size")
        declared += size
    if declared > MAX_SPIKE_BYTES:
        raise SpikeDigestError(f"inventory exceeds the {MAX_SPIKE_BYTES} byte cap")
    entries: list[InventoryEntry] = []
    for node in wanted:
        blob_sha = node.get("sha")
        if not isinstance(blob_sha, str):
            raise SpikeDigestError(f"{node['path']!r} has no blob sha")
        blob = await _get_json(client, f"{base}/blobs/{blob_sha}", headers)
        content = blob.get("content")
        if blob.get("encoding") != "base64" or not isinstance(content, str):
            raise SpikeDigestError(f"{node['path']!r} is not an inlined blob")
        try:
            data = base64.b64decode(content.replace("\n", ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SpikeDigestError(f"{node['path']!r}: undecodable content") from exc
        if len(data) != node["size"] or _git_blob_sha(sha, data) != blob_sha:
            raise SpikeDigestError(f"{node['path']!r}: bytes do not match blob {blob_sha}")
        entries.append(InventoryEntry(path=node["path"], content=data, mode=node["mode"]))
    return entries


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------


def _map(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewError(CODE_MALFORMED, f"{where} must be a mapping")
    return value


def _keys(data: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ReviewError(CODE_UNKNOWN_KEY, f"{where}: unknown key(s) {sorted(unknown)!r}")


def _str(value: Any, where: str, *, pattern: re.Pattern[str] | None = None, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty):
        raise ReviewError(CODE_MALFORMED, f"{where} must be a non-empty string")
    if len(value) > MAX_STR:
        raise ReviewError(CODE_MALFORMED, f"{where} exceeds {MAX_STR} characters")
    if value and pattern is not None and not pattern.fullmatch(value):
        raise ReviewError(CODE_MALFORMED, f"{where} has an invalid shape")
    return value


def _choice(value: Any, allowed: Sequence[str], where: str) -> str:
    if value not in allowed:
        raise ReviewError(CODE_MALFORMED, f"{where} must be one of {list(allowed)!r}")
    return str(value)


def _str_tuple(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_ACTORS:
        raise ReviewError(CODE_MALFORMED, f"{where} must be a list of at most {MAX_ACTORS} strings")
    return tuple(sorted({_str(v, where) for v in value}))


def _check_api(data: Mapping[str, Any], kind: str) -> None:
    if data.get("api_version") != API_VERSION or data.get("kind") != kind:
        raise ReviewError(CODE_MALFORMED, f"expected {API_VERSION} {kind}")


# ---------------------------------------------------------------------------
# Subject
# ---------------------------------------------------------------------------

_SPIKE_FIELDS = (
    "repository", "report_root", "report_path", "source_sha", "base_sha", "head_sha",
    "inventory_digest", "digest_version",
)  # fmt: skip
_PROPOSAL_FIELDS = ("service", "slug", "gitops_commit", "content_digest", "approval_ref")
_SUBJECT_KEYS = frozenset({"kind", *_SPIKE_FIELDS, *_PROPOSAL_FIELDS})


@dataclass(frozen=True)
class ReviewSubject:
    """The exact, digest-bound thing under review, tagged by ``kind``."""

    kind: str
    repository: str = ""
    report_root: str = ""
    report_path: str = ""
    source_sha: str = ""
    base_sha: str = ""
    head_sha: str = ""
    inventory_digest: str = ""
    digest_version: str = ""
    service: str = ""
    slug: str = ""
    gitops_commit: str = ""
    content_digest: str = ""
    approval_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        fields = _SPIKE_FIELDS if self.kind == SUBJECT_SPIKE_REPORT else _PROPOSAL_FIELDS
        out = {"kind": self.kind}
        for name in fields:
            value = getattr(self, name)
            if value or name != "approval_ref":
                out[name] = value
        return out

    def identity(self) -> tuple[str, ...]:
        """What is under review, independent of which bytes."""
        if self.kind == SUBJECT_SPIKE_REPORT:
            return (self.kind, self.repository, self.report_root, self.report_path)
        return (self.kind, self.service, self.slug)

    def binding(self) -> dict[str, Any]:
        """The bytes a receipt is valid for. A proposal's ``gitops_commit`` and
        ``approval_ref`` are provenance only: equal bytes at a later commit are
        the same proposal (mctl-agents#701 semantics)."""
        if self.kind == SUBJECT_PROPOSAL:
            return {"kind": self.kind, "service": self.service, "slug": self.slug,
                    "content_digest": self.content_digest}  # fmt: skip
        return self.to_dict()

    @classmethod
    def from_dict(cls, data: Any) -> ReviewSubject:
        mapping = _map(data, "subject")
        _keys(mapping, _SUBJECT_KEYS, "subject")
        kind = _choice(mapping.get("kind"), sorted(SUBJECT_KINDS), "subject.kind")
        allowed = _SPIKE_FIELDS if kind == SUBJECT_SPIKE_REPORT else _PROPOSAL_FIELDS
        extra = [k for k in _SUBJECT_KEYS - {"kind", *allowed} if k in mapping]
        if extra:
            raise ReviewError(CODE_BAD_SUBJECT, f"{kind} subject cannot carry {sorted(extra)!r}")
        values: dict[str, str] = {}
        for name in allowed:
            if name == "approval_ref":
                values[name] = _str(mapping.get(name, ""), "subject.approval_ref", pattern=GRANT_REF_RE, empty=True)
            else:
                values[name] = _str(mapping.get(name), f"subject.{name}")
        subject = cls(kind=kind, **values)
        subject._validate()
        return subject

    def _validate(self) -> None:
        if self.kind == SUBJECT_SPIKE_REPORT:
            if not _REPO_RE.fullmatch(self.repository):
                raise ReviewError(CODE_BAD_SUBJECT, "repository must be owner/name")
            try:
                _check_path(self.report_root)
                _check_path(self.report_path)
            except SpikeDigestError as exc:
                raise ReviewError(CODE_BAD_SUBJECT, str(exc)) from exc
            if not self.report_path.endswith(".md"):
                raise ReviewError(CODE_BAD_SUBJECT, "report_path must be a .md file")
            for name in ("source_sha", "base_sha", "head_sha"):
                if not _SHA_RE.fullmatch(getattr(self, name)):
                    raise ReviewError(CODE_BAD_SUBJECT, f"{name} must be a full lowercase commit SHA")
            if not _DIGEST_RE.fullmatch(self.inventory_digest):
                raise ReviewError(CODE_BAD_SUBJECT, "inventory_digest must be sha256:<64 hex>")
            if self.digest_version != SPIKE_DIGEST_VERSION:
                raise ReviewError(CODE_BAD_SUBJECT, f"digest_version must be {SPIKE_DIGEST_VERSION}")
        else:
            if not _SERVICE_RE.fullmatch(self.service) or not _SLUG_RE.fullmatch(self.slug):
                raise ReviewError(CODE_BAD_SUBJECT, "service and slug have an invalid shape")
            if not _SHA_RE.fullmatch(self.gitops_commit):
                raise ReviewError(CODE_BAD_SUBJECT, "gitops_commit must be a full lowercase commit SHA")
            if not _DIGEST_RE.fullmatch(self.content_digest):
                raise ReviewError(CODE_BAD_SUBJECT, "content_digest must be sha256:<64 hex>")


def spike_subject(
    *,
    repository: str,
    report_root: str,
    report_path: str,
    source_sha: str,
    base_sha: str,
    head_sha: str,
    entries: Sequence[InventoryEntry],
) -> ReviewSubject:
    """A spike subject whose ``inventory_digest`` comes from ``entries``."""
    subject = ReviewSubject(
        kind=SUBJECT_SPIKE_REPORT,
        repository=repository,
        report_root=report_root,
        report_path=report_path,
        source_sha=source_sha,
        base_sha=base_sha,
        head_sha=head_sha,
        inventory_digest=spike_report_digest(entries, report_path=report_path),
        digest_version=SPIKE_DIGEST_VERSION,
    )
    subject._validate()
    return subject


def proposal_subject(
    *,
    service: str,
    slug: str,
    gitops_commit: str,
    files: Mapping[str, bytes | None],
    approval_ref: str = "",
) -> ReviewSubject:
    """A proposal subject. The digest is ``proposal_state.content_digest_of``;
    there is no second proposal digest."""
    subject = ReviewSubject(
        kind=SUBJECT_PROPOSAL,
        service=service,
        slug=slug,
        gitops_commit=gitops_commit,
        content_digest=content_digest_of(files),
        approval_ref=approval_ref,
    )
    subject._validate()
    if approval_ref:
        _str(approval_ref, "subject.approval_ref", pattern=GRANT_REF_RE)
    return subject


# ---------------------------------------------------------------------------
# Request and receipt
# ---------------------------------------------------------------------------

_REQUEST_KEYS = frozenset({
    "api_version", "kind", "request_id", "content_hash", "subject", "rubric", "mode", "author_actors",
    "authoring_execution_id", "authoring_attempt_ids", "authoring_lineage_root", "nonce", "created_at",
})  # fmt: skip
_RECEIPT_KEYS = frozenset({
    "api_version", "kind", "receipt_id", "content_hash", "request_id", "subject", "rubric", "outcome",
    "criteria", "findings_ref", "mode", "assurance", "reviewer_actor", "reviewer_execution_id", "attempt_id",
    "lineage_root", "nonce", "evidence_ref", "created_at",
})  # fmt: skip


def _created_at(mapping: Mapping[str, Any]) -> str:
    return _str(mapping.get("created_at", ""), "created_at", pattern=_CREATED_AT_RE, empty=True)


def _seal_hash(payload: Mapping[str, Any]) -> str:
    return hash_bytes(canonical_json(dict(payload)))


@dataclass(frozen=True)
class ReviewRequest:
    """A request to review one exact subject. Build with ``seal_request``."""

    subject: ReviewSubject
    rubric: str
    mode: str
    author_actors: tuple[str, ...]
    nonce: str
    authoring_execution_id: str = ""
    authoring_attempt_ids: tuple[str, ...] = ()
    authoring_lineage_root: str = ""
    created_at: str = ""
    content_hash: str = ""
    request_id: str = ""

    def _payload(self) -> dict[str, Any]:
        return {
            "api_version": API_VERSION,
            "kind": REQUEST_KIND,
            "subject": self.subject.to_dict(),
            "rubric": self.rubric,
            "mode": self.mode,
            "author_actors": list(self.author_actors),
            "authoring_execution_id": self.authoring_execution_id,
            "authoring_attempt_ids": list(self.authoring_attempt_ids),
            "authoring_lineage_root": self.authoring_lineage_root,
            "nonce": self.nonce,
        }

    def validate(self) -> None:
        if self.rubric != RUBRIC_FOR_SUBJECT[self.subject.kind]:
            raise ReviewError(CODE_RUBRIC_MISMATCH, f"rubric {self.rubric!r} does not fit {self.subject.kind}")
        _choice(self.mode, MODES, "mode")
        _str(self.nonce, "nonce")
        # A worker request without authoring_execution_id is representable:
        # the gate answers `unknown` for it, and the Tier B adapter refuses it.
        if self.authoring_execution_id:
            _str(self.authoring_execution_id, "authoring_execution_id", pattern=_EXECUTION_ID_RE)
        # Same bounds from_dict applies, so seal_request and the wire round-trip agree.
        if len(self.author_actors) > MAX_ACTORS or len(self.authoring_attempt_ids) > MAX_ACTORS:
            raise ReviewError(CODE_MALFORMED, f"actor and attempt lists hold at most {MAX_ACTORS} entries")
        for value in self.author_actors:
            _str(value, "author_actors")
        for value in self.authoring_attempt_ids:
            _str(value, "authoring_attempt_ids")
        _str(self.authoring_lineage_root, "authoring_lineage_root", empty=True)

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "request_id": self.request_id, "content_hash": self.content_hash,
                "created_at": self.created_at}  # fmt: skip

    @classmethod
    def from_dict(cls, data: Any) -> ReviewRequest:
        mapping = _map(data, "request")
        _keys(mapping, _REQUEST_KEYS, "request")
        _check_api(mapping, REQUEST_KIND)
        request = cls(
            subject=ReviewSubject.from_dict(mapping.get("subject")),
            rubric=_choice(mapping.get("rubric"), sorted(RUBRICS), "rubric"),
            mode=_choice(mapping.get("mode"), MODES, "mode"),
            author_actors=_str_tuple(mapping.get("author_actors", []), "author_actors"),
            nonce=_str(mapping.get("nonce"), "nonce"),
            authoring_execution_id=_str(
                mapping.get("authoring_execution_id", ""), "authoring_execution_id", empty=True
            ),
            authoring_attempt_ids=_str_tuple(mapping.get("authoring_attempt_ids", []), "authoring_attempt_ids"),
            authoring_lineage_root=_str(
                mapping.get("authoring_lineage_root", ""), "authoring_lineage_root", empty=True
            ),
            created_at=_created_at(mapping),
            content_hash=_str(mapping.get("content_hash", ""), "content_hash", empty=True),
            request_id=_str(mapping.get("request_id", ""), "request_id", empty=True),
        )
        request.validate()
        if request.content_hash or request.request_id:
            _check_identity(request.content_hash, request.request_id, _seal_hash(request._payload()), REQUEST_ID_PREFIX)
        return request


def seal_request(
    *,
    subject: ReviewSubject,
    mode: str,
    author_actors: Sequence[str],
    nonce: str,
    authoring_execution_id: str = "",
    authoring_attempt_ids: Sequence[str] = (),
    authoring_lineage_root: str = "",
    created_at: str = "",
) -> ReviewRequest:
    """The only constructor that fills ``content_hash`` and ``request_id``."""
    draft = ReviewRequest(
        subject=subject,
        rubric=RUBRIC_FOR_SUBJECT[subject.kind],
        mode=mode,
        author_actors=tuple(sorted(set(author_actors))),
        nonce=nonce,
        authoring_execution_id=authoring_execution_id,
        authoring_attempt_ids=tuple(sorted(set(authoring_attempt_ids))),
        authoring_lineage_root=authoring_lineage_root,
        created_at=created_at,
    )
    draft.validate()
    content_hash = _seal_hash(draft._payload())
    return ReviewRequest(**{**draft.__dict__, "content_hash": content_hash,
                            "request_id": REQUEST_ID_PREFIX + content_hash[7:23]})  # fmt: skip


def _check_identity(content_hash: str, doc_id: str, expected_hash: str, prefix: str) -> None:
    if content_hash != expected_hash or doc_id != prefix + expected_hash[7:23]:
        raise ReviewError(CODE_BAD_IDENTITY, "content_hash or id does not match the document")


@dataclass(frozen=True)
class Criterion:
    id: str
    status: str


@dataclass(frozen=True)
class ReviewReceipt:
    """The answer to a ``ReviewRequest``. Build with ``seal_receipt``.

    ``reviewer_actor``, ``reviewer_execution_id`` and ``assurance`` are
    authoritative only as filled by the Tier B server; here they are shape
    validated. ``findings_ref`` is a hash pointer to private findings, never
    inline text."""

    request_id: str
    subject: ReviewSubject
    rubric: str
    outcome: str
    criteria: tuple[Criterion, ...]
    mode: str
    nonce: str
    findings_ref: ArtifactRef = field(default_factory=ArtifactRef)
    assurance: str = "advisory"
    reviewer_actor: str = ""
    reviewer_execution_id: str = ""
    attempt_id: str = ""
    lineage_root: str = ""
    evidence_ref: str = ""
    created_at: str = ""
    content_hash: str = ""
    receipt_id: str = ""

    def _payload(self) -> dict[str, Any]:
        return {
            "api_version": API_VERSION,
            "kind": RECEIPT_KIND,
            "request_id": self.request_id,
            "subject": self.subject.to_dict(),
            "rubric": self.rubric,
            "outcome": self.outcome,
            "criteria": [{"id": c.id, "status": c.status} for c in self.criteria],
            "findings_ref": self.findings_ref.to_dict(),
            "mode": self.mode,
            "assurance": self.assurance,
            "reviewer_actor": self.reviewer_actor,
            "reviewer_execution_id": self.reviewer_execution_id,
            "attempt_id": self.attempt_id,
            "lineage_root": self.lineage_root,
            "nonce": self.nonce,
            "evidence_ref": self.evidence_ref,
        }

    def validate(self) -> None:
        _choice(self.outcome, OUTCOMES, "outcome")
        _choice(self.mode, MODES, "mode")
        if self.rubric != RUBRIC_FOR_SUBJECT[self.subject.kind]:
            raise ReviewError(CODE_RUBRIC_MISMATCH, f"rubric {self.rubric!r} does not fit {self.subject.kind}")
        if self.assurance != ("advisory" if self.mode == "manual" else "independent"):
            raise ReviewError(CODE_MALFORMED, "assurance must be advisory for manual mode and independent for worker")
        required = RUBRICS[self.rubric]
        ids = [c.id for c in self.criteria]
        if len(set(ids)) != len(ids) or not set(ids) <= set(required):
            raise ReviewError(CODE_MALFORMED, "criteria must be distinct ids of the rubric")
        for criterion in self.criteria:
            _choice(criterion.status, CRITERION_STATUSES, "criterion.status")
        if self.outcome == "PASS":
            if set(ids) != set(required) or any(c.status != "met" for c in self.criteria):
                raise ReviewError(CODE_PASS_WITH_UNOBSERVED, "PASS needs every rubric criterion observed and met")
        elif self.outcome == "CHANGES_REQUESTED" and any(c.status == "unobserved" for c in self.criteria):
            raise ReviewError(CODE_UNOBSERVED_OUTCOME, "an unobserved criterion forces UNOBSERVABLE or NEEDS_HUMAN")
        _str(self.request_id, "request_id", pattern=re.compile(r"rr_[0-9a-f]{16}"))
        _str(self.nonce, "nonce")
        for name in ("reviewer_actor", "reviewer_execution_id", "attempt_id", "lineage_root"):
            _str(getattr(self, name), name, empty=True)
        if self.evidence_ref:
            _str(self.evidence_ref, "evidence_ref", pattern=_EVIDENCE_ID_RE)
        if self.findings_ref.content_hash:
            _str(self.findings_ref.content_hash, "findings_ref.content_hash", pattern=_DIGEST_RE)

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "receipt_id": self.receipt_id, "content_hash": self.content_hash,
                "created_at": self.created_at}  # fmt: skip

    @classmethod
    def from_dict(cls, data: Any) -> ReviewReceipt:
        mapping = _map(data, "receipt")
        _keys(mapping, _RECEIPT_KEYS, "receipt")
        _check_api(mapping, RECEIPT_KIND)
        criteria: list[Criterion] = []
        raw_criteria = mapping.get("criteria")
        if not isinstance(raw_criteria, list):
            raise ReviewError(CODE_MALFORMED, "criteria must be a list")
        for raw in raw_criteria:
            item = _map(raw, "criterion")
            _keys(item, frozenset({"id", "status"}), "criterion")
            criteria.append(
                Criterion(id=_str(item.get("id"), "criterion.id"), status=_str(item.get("status"), "criterion.status"))
            )
        try:
            findings = ArtifactRef.from_dict(mapping.get("findings_ref", {}))
        except ValueError as exc:
            raise ReviewError(CODE_MALFORMED, f"findings_ref: {exc}") from exc
        receipt = cls(
            request_id=_str(mapping.get("request_id"), "request_id"),
            subject=ReviewSubject.from_dict(mapping.get("subject")),
            rubric=_choice(mapping.get("rubric"), sorted(RUBRICS), "rubric"),
            outcome=_str(mapping.get("outcome"), "outcome"),
            criteria=tuple(criteria),
            mode=_str(mapping.get("mode"), "mode"),
            nonce=_str(mapping.get("nonce"), "nonce"),
            findings_ref=findings,
            assurance=_str(mapping.get("assurance"), "assurance"),
            reviewer_actor=_str(mapping.get("reviewer_actor", ""), "reviewer_actor", empty=True),
            reviewer_execution_id=_str(mapping.get("reviewer_execution_id", ""), "reviewer_execution_id", empty=True),
            attempt_id=_str(mapping.get("attempt_id", ""), "attempt_id", empty=True),
            lineage_root=_str(mapping.get("lineage_root", ""), "lineage_root", empty=True),
            evidence_ref=_str(mapping.get("evidence_ref", ""), "evidence_ref", empty=True),
            created_at=_created_at(mapping),
            content_hash=_str(mapping.get("content_hash", ""), "content_hash", empty=True),
            receipt_id=_str(mapping.get("receipt_id", ""), "receipt_id", empty=True),
        )
        receipt.validate()
        if receipt.content_hash or receipt.receipt_id:
            _check_identity(receipt.content_hash, receipt.receipt_id, _seal_hash(receipt._payload()), RECEIPT_ID_PREFIX)
        return receipt


def seal_receipt(
    *,
    request: ReviewRequest,
    outcome: str,
    criteria: Sequence[Criterion],
    findings_ref: ArtifactRef | None = None,
    reviewer_actor: str = "",
    reviewer_execution_id: str = "",
    attempt_id: str = "",
    lineage_root: str = "",
    evidence_ref: str = "",
    created_at: str = "",
) -> ReviewReceipt:
    """The only constructor that fills identity. The receipt takes the
    request's subject, rubric, mode and nonce; manual mode forces
    ``assurance=advisory``."""
    draft = ReviewReceipt(
        request_id=request.request_id,
        subject=request.subject,
        rubric=request.rubric,
        outcome=outcome,
        criteria=tuple(criteria),
        mode=request.mode,
        nonce=request.nonce,
        findings_ref=findings_ref or ArtifactRef(),
        assurance="advisory" if request.mode == "manual" else "independent",
        reviewer_actor=reviewer_actor,
        reviewer_execution_id=reviewer_execution_id,
        attempt_id=attempt_id,
        lineage_root=lineage_root,
        evidence_ref=evidence_ref,
        created_at=created_at,
    )
    draft.validate()
    content_hash = _seal_hash(draft._payload())
    return ReviewReceipt(**{**draft.__dict__, "content_hash": content_hash,
                            "receipt_id": RECEIPT_ID_PREFIX + content_hash[7:23]})  # fmt: skip


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def evaluate_review_gate(
    *,
    live: ReviewSubject | None,
    request: ReviewRequest | None,
    receipts: Sequence[ReviewReceipt],
    required_assurance: str = "independent",
) -> str:
    """Answer whether the live subject has a current, independent review.

    ``live`` is the subject as the caller just observed it (``None`` when the
    revision or digest could not be observed). ``request`` carries the
    authoring facts for that live revision. ``receipts`` is the complete
    candidate pool from Tier B in server ingest order, oldest first. There is
    no GitHub review input.

    Order: unknown live -> pool by exact binding (stale/missing) -> request
    sealed, bound to the live binding and with an observable authoring
    execution (else unknown) -> receipts of this request only (else missing)
    -> independence, where an unobserved worker identity does not count
    (not_independent) -> assurance (advisory_only) -> latest receipt's outcome.
    """
    if required_assurance not in REQUIRED_ASSURANCES:
        raise ValueError(f"required_assurance must be one of {REQUIRED_ASSURANCES}")
    if live is None:
        return GATE_UNKNOWN
    same = [r for r in receipts if r.subject.identity() == live.identity()]
    pool = [r for r in same if r.subject.binding() == live.binding()]
    if not pool:
        return GATE_STALE if same else GATE_MISSING
    # The authoring facts must describe this exact revision: a request sealed
    # for an older binding says nothing about who wrote the live one.
    if request is None or request.subject.binding() != live.binding() or not request.request_id:
        return GATE_UNKNOWN
    if request.mode == "worker" and not request.authoring_execution_id:
        return GATE_UNKNOWN
    pool = [r for r in pool if r.request_id == request.request_id]
    if not pool:
        return GATE_MISSING
    authors = {a.casefold() for a in request.author_actors}
    attempts = set(request.authoring_attempt_ids)

    def independent(r: ReviewReceipt) -> bool:
        if not r.reviewer_actor or r.reviewer_actor.casefold() in authors:
            return False
        # A worker receipt that cannot name its execution, attempt and lineage
        # cannot be compared with the author's: unobserved is not independent.
        if r.mode == "worker" and not (r.reviewer_execution_id and r.attempt_id and r.lineage_root):
            return False
        if request.authoring_execution_id and r.reviewer_execution_id == request.authoring_execution_id:
            return False
        if r.attempt_id and r.attempt_id in attempts:
            return False
        return not (request.authoring_lineage_root and r.lineage_root == request.authoring_lineage_root)

    pool = [r for r in pool if independent(r)]
    if not pool:
        return GATE_NOT_INDEPENDENT
    if required_assurance == "independent":
        pool = [r for r in pool if r.assurance == "independent"]
        if not pool:
            return GATE_ADVISORY_ONLY
    return _OUTCOME_TO_GATE[pool[-1].outcome]
