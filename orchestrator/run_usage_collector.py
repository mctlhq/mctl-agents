"""Usage-collector — pulls reviewer `model-usage-records` artifacts into the
mctl-api usage ledger (mctlhq/.github#50, mctlhq/mctl-agents#506, ADR-012).

The reviewer stage (`claude-review.yml`, mctlhq/.github#126) runs in GitHub
Actions, not inside mctl-agents, and publishes its ADR-012 usage records as
the `model-usage-records` workflow artifact rather than posting them — the
ingest endpoint is admin-only-adjacent (a confined writer principal) and an
admin-scoped credential does not belong in every caller repository as a
GitHub secret. This module is the collector side of that split: a
model-free periodic sweep, runnable on the existing image, that

    for each configured mctlhq repository:
        list its in-window model-usage-records artifacts
        download each one (bounded, binary-safe)
        sanitise its candidate records onto the ADR-012 allowlist
        POST that repository's sanitised records to mctl-api, chunked

It mints no record id — mctl-api derives `(session_id, result_uuid,
model_key)` itself and rejects a supplied id that disagrees
(`Record.EnsureID`), so an `id` field on a candidate record is dropped, never
forwarded. It adds no correlation from its own pod environment: the pod's
`WORKFLOW_NAME` / `WORKFLOW_TEMPORAL_WORKFLOW_ID` / `WORKFLOW_WORK_ITEM_ID`
describe the collector's own Argo run, not the review, so joining a reviewer
row to them would attribute reviewer spend to the wrong execution. It never
writes to GitHub: every call this module makes is `gh api` against a `GET`
endpoint (listing, and the artifact zip download), and it never runs `gh
issue`, `gh pr`, `gh run` or any `git push`.

Statelessness is the idempotency design: the collector keeps no cursor and
no seen-set. Re-collecting the same artifact is a no-op because mctl-api's
insert-or-ignore on the derived id makes a second delivery add no row — so
"re-read one artifact" and "re-run the whole sweep" are the same operation,
and a wider `--lookback-days` backfill is safe rather than a double count.

Failure handling follows `orchestrator/run_issue_poller.py`'s precedent:
per-repository and per-artifact failures are logged and counted, and the
sweep continues; the process exits non-zero only when `gh` cannot
authenticate at all (checked once, up front), so the CronWorkflow surfaces a
genuine credential outage without flapping on one bad repository or a
one-off download error.

Usage:
    python -m orchestrator.run_usage_collector
    python -m orchestrator.run_usage_collector --dry-run
    python -m orchestrator.run_usage_collector --lookback-days 90
    python -m orchestrator.run_usage_collector --repo mctlhq/.github

Auth:
    GITHUB_TOKEN from env (`gh` honors it) — needs read access to Actions
    artifacts on the swept repositories. GITHUB_TOKEN_FILE, if set, is
    re-read before every `gh` call (orchestrator.github_token).
    MCTL_USAGE_WRITER_TOKEN / MCTL_API_BASE_URL — see
    orchestrator/usage_ledger.py; unset, the collector still discovers and
    (with --dry-run) reports, but posts nothing.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from config.settings import SERVICES
from orchestrator.github_token import refresh_github_token
from orchestrator.proc import CommandFailed, run_capturing
from orchestrator.usage_ledger import (
    BASE_URL_ENV,
    DEFAULT_BASE_URL,
    DEVLOOP_STAGES,
    INGEST_PATH,
    SCHEMA_VERSION,
    TARGET_REPO_RE,
    TOKEN_ENV,
)

ARTIFACT_NAME = "model-usage-records"
ARTIFACT_MEMBER = "model-usage-records.json"
DEFAULT_LOOKBACK_DAYS = 3
DEFAULT_MAX_PAGES = 5
ARTIFACTS_PER_PAGE = 100
MAX_ARTIFACT_BYTES = 1 << 20
MAX_BATCH = 500
MAX_BODY_BYTES = 1 << 20

REQUEST_TIMEOUT_SECONDS = 10.0
DOWNLOAD_TIMEOUT_SECONDS = 120.0

# The ADR-012 fields this collector forwards unchanged. `id` is deliberately
# absent: mctl-api derives it from (session_id, result_uuid, model_key) and
# `Record.EnsureID` rejects a supplied id that disagrees, which would fail
# the whole (single-transaction) batch. `calculated_cost`, `pricing_version`
# and `provider_reported_cost` are also deliberately absent — mctl-api prices
# the token counts itself at ingest.
_ALLOWED_FIELDS = frozenset({
    "schema_version",
    "session_id", "result_uuid", "model_key", "canonical_model", "provider",
    "agent", "devloop_stage", "target_repo", "issue_number", "pr_number",
    "work_item_id", "trace_id", "span_id",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "web_search_requests",
    "outcome", "api_error_status", "stop_reason", "terminal_reason",
    "num_turns", "duration_api_ms", "retry_attempt", "recorded_at",
})

# The two shapes `_ALLOWED_FIELDS` (minus session_id/model_key/schema_version,
# checked separately above) can take. mctl-api's ingest is one transaction, so
# a field of the wrong Python type is dropped on its own, with a warning,
# rather than forwarded — otherwise it would fail every record chunked into
# the same batch as the malformed one (see `_chunk_records`), not just the
# record it came from.
_STRING_FIELDS = frozenset({
    "result_uuid", "canonical_model", "provider", "agent", "devloop_stage",
    "target_repo", "work_item_id", "trace_id", "span_id",
    "outcome", "api_error_status", "stop_reason", "terminal_reason", "recorded_at",
})
_INT_FIELDS = frozenset({
    "issue_number", "pr_number",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "web_search_requests",
    "num_turns", "duration_api_ms", "retry_attempt",
})

Post = Callable[[str, dict[str, Any], dict[str, str]], httpx.Response]


@dataclass
class Artifact:
    id: int
    created_at: str


@dataclass
class CollectResult:
    repositories: int = 0
    artifacts_found: int = 0
    artifacts_read: int = 0
    artifacts_skipped: int = 0
    records_posted: int = 0
    records_deduped: int = 0
    failures: int = 0

    def summary(self) -> str:
        return (
            f"repositories={self.repositories} artifacts_found={self.artifacts_found} "
            f"artifacts_read={self.artifacts_read} artifacts_skipped={self.artifacts_skipped} "
            f"records_posted={self.records_posted} records_deduped={self.records_deduped} "
            f"failures={self.failures}"
        )


@dataclass
class DeliveryResult:
    accepted: int = 0
    deduped: int = 0
    undelivered: int = 0
    # Chunks that were attempted (token + base_url both valid) and failed —
    # distinct from `undelivered`, which also counts records that were never
    # attempted at all (no token, or a non-https base_url). Rolled into
    # CollectResult.failures by collect(); the "no token"/"non-https" cases
    # are not, matching "log one warning, post nothing, exit successfully".
    failed_chunks: int = 0


def _run_gh(args: list[str]) -> subprocess.CompletedProcess:
    """`gh {args}`, with a freshly re-read GITHUB_TOKEN (see
    orchestrator.github_token) — a long backfill can outlive the
    installation token's TTL, and short crons get a no-op refresh."""
    refresh_github_token()
    return run_capturing(["gh", *args])


def _check_gh_auth() -> None:
    """Raise if `gh` cannot authenticate at all — a genuine credential
    outage the caller should treat as a global failure, not a per-repository
    one to log and skip past.

    Uses GraphQL's `viewer { login }` rather than `gh api user`: the latter
    is REST `/user`, which needs a user-to-server OAuth token and 403s for a
    GitHub App installation token — exactly what this collector's
    `GITHUB_TOKEN` is (see orchestrator/run_issue_directive_poller.py's
    `_current_gh_login` for the same finding).
    """
    _run_gh(["api", "graphql", "-f", "query=query { viewer { login } }"])


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def list_artifacts(repo: str, cutoff: datetime, max_pages: int = DEFAULT_MAX_PAGES) -> list[Artifact]:
    """Every `model-usage-records` artifact of `repo` created at or after
    `cutoff`, across up to `max_pages` pages of `per_page=100`.

    Never breaks early on the first out-of-window entry: the listing is
    ordered by artifact id (upload order), not by `created_at` — observed on
    the real API, an artifact created 23:43 was listed before one created
    23:51 — so a later entry on the same page can still be in-window after
    an earlier one was not. `expired` artifacts are dropped unconditionally.
    Paging stops at `max_pages` or a short page (fewer than
    `ARTIFACTS_PER_PAGE` rows).

    A `gh` command failure (auth, network) propagates to the caller, which
    is the point: it is what lets a genuine outage be told apart from
    "this repository has no artifacts". Malformed response JSON from a
    successful `gh` call is instead logged and treated as the end of this
    repository's listing — `gh` did its job; the payload just was not the
    shape expected.
    """
    found: list[Artifact] = []
    for page in range(1, max_pages + 1):
        # Query parameters go in the URL, not as `-f` fields: `gh api`
        # defaults to GET only when it has no fields to send — any `-f`/`-F`
        # turns an unmethoded call into a POST (its body carrying the
        # "fields"), which silently 404s/misbehaves against a listing
        # endpoint. Kept as a literal query string, not `-f`, precisely so
        # this call stays GET without ever writing `--method`/`-X` in this
        # read-only module (see tests/test_usage_collector_readonly.py).
        proc = _run_gh([
            "api",
            f"repos/{repo}/actions/artifacts?name={ARTIFACT_NAME}&per_page={ARTIFACTS_PER_PAGE}&page={page}",
        ])
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            print(f"WARN: {repo} artifact listing page {page} was not valid JSON — stopping this repository's listing")
            break
        rows = payload.get("artifacts") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            print(f"WARN: {repo} artifact listing page {page} had no 'artifacts' list — stopping this listing")
            break
        for row in rows:
            if not isinstance(row, dict) or row.get("expired"):
                continue
            created_at = _parse_timestamp(str(row.get("created_at") or ""))
            if created_at is None or created_at < cutoff:
                continue
            artifact_id = row.get("id")
            if isinstance(artifact_id, int):
                found.append(Artifact(id=artifact_id, created_at=str(row.get("created_at"))))
        if len(rows) < ARTIFACTS_PER_PAGE:
            break
    return found


def download_records(repo: str, artifact_id: int, max_bytes: int = MAX_ARTIFACT_BYTES) -> list[dict[str, Any]] | None:
    """The candidate records inside artifact `artifact_id`'s
    `model-usage-records.json` member — `None` when the artifact is skipped
    for any reason (oversize, wrong shape, not a zip, download failure).

    Downloaded with `gh api …/zip` writing straight to a temp file's binary
    handle, not through `run_capturing` (whose text mode would corrupt the
    zip bytes with `errors="replace"`). Refuses a file over `max_bytes`
    before opening it as a zip, and refuses any member declaring an
    uncompressed size over `max_bytes` before reading it — neither a large
    artifact nor a zip bomb is ever expanded into memory. Reads only the
    `model-usage-records.json` member. The artifact is treated as untrusted
    input: it is influenced by whatever ran the reviewer workflow, even
    though the workflow that writes it is our own.
    """
    refresh_github_token()
    fd, path = tempfile.mkstemp(prefix="usage-artifact-", suffix=".zip")
    try:
        with os.fdopen(fd, "wb") as handle:
            try:
                # S603: fixed argv plus repo/artifact_id, never shell.
                # S607: `gh` resolved via PATH, like every other call site.
                subprocess.run(  # noqa: S603
                    ["gh", "api", f"repos/{repo}/actions/artifacts/{artifact_id}/zip"],  # noqa: S607
                    stdout=handle, check=True, timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )
            except subprocess.CalledProcessError as exc:
                print(f"WARN: could not download {repo} artifact {artifact_id} ({exc})")
                return None
            except subprocess.TimeoutExpired:
                print(f"WARN: download of {repo} artifact {artifact_id} timed out")
                return None

        size = os.path.getsize(path)
        if size > max_bytes:
            print(f"WARN: {repo} artifact {artifact_id} is {size} bytes (> {max_bytes}) — skipped, not read")
            return None

        try:
            with zipfile.ZipFile(path) as zf:
                try:
                    info = zf.getinfo(ARTIFACT_MEMBER)
                except KeyError:
                    print(f"WARN: {repo} artifact {artifact_id} has no {ARTIFACT_MEMBER} member — skipped")
                    return None
                if info.file_size > max_bytes:
                    print(
                        f"WARN: {repo} artifact {artifact_id}'s {ARTIFACT_MEMBER} declares "
                        f"{info.file_size} bytes uncompressed (> {max_bytes}) — skipped, not read"
                    )
                    return None
                raw = zf.read(info)
        except zipfile.BadZipFile:
            print(f"WARN: {repo} artifact {artifact_id} is not a zip — skipped")
            return None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        print(f"WARN: {repo} artifact {artifact_id}'s {ARTIFACT_MEMBER} did not parse as JSON — skipped")
        return None
    records = parsed.get("records") if isinstance(parsed, dict) else None
    if not isinstance(records, list):
        print(f"WARN: {repo} artifact {artifact_id}'s {ARTIFACT_MEMBER} has no 'records' list — skipped")
        return None
    return [r for r in records if isinstance(r, dict)]


def sanitise(raw: dict[str, Any]) -> dict[str, Any] | None:
    """`raw` projected onto the ADR-012 field allowlist, or `None` (with a
    warning) when it fails a required check.

    Drops `id` and every key not in `_ALLOWED_FIELDS` — an unrecognised key,
    including a future one, is dropped rather than forwarded. Preserves
    every allowed field exactly as given (an absent counter stays absent,
    never becomes 0, and nothing here adds `argo_workflow_name`,
    `temporal_workflow_id`, `work_item_id` or `execution_id` from this
    process's own environment) EXCEPT when a field fails a check mctl-api
    itself applies at ingest: a Python type mismatch
    (`_STRING_FIELDS`/`_INT_FIELDS`), a `devloop_stage` outside the closed
    v1 vocabulary (`DEVLOOP_STAGES`, ADR-012 invariant 10 and its
    2026-09-24 amendment — "free text, wrong case, a non-string, empty"),
    a `target_repo` that is not `owner/name` (`TARGET_REPO_RE`, the same
    pattern `orchestrator.usage_ledger._checked_correlation` enforces for
    the other producer), or an `issue_number`/`pr_number` that is not a
    positive number or has no accompanying `target_repo` to be read
    against. In every one of those cases the single offending field is
    dropped, with a warning, rather than forwarded — the artifact is
    untrusted input, and the ingest is one transaction, so one malformed
    field would otherwise fail every record chunked into the same batch,
    not just the record it came from.
    """
    session_id = raw.get("session_id")
    if not (isinstance(session_id, str) and session_id.strip()):
        print("WARN: dropping a candidate record with no non-empty session_id")
        return None
    model_key = raw.get("model_key")
    if not (isinstance(model_key, str) and model_key.strip()):
        print(f"WARN: dropping record {session_id!r} with no non-empty model_key")
        return None
    schema_version = raw.get("schema_version")
    if schema_version is not None and (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SCHEMA_VERSION
    ):
        print(
            f"WARN: dropping record {session_id!r}/{model_key!r} with "
            f"schema_version={schema_version!r} (expected {SCHEMA_VERSION})"
        )
        return None
    sanitised: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _ALLOWED_FIELDS:
            continue
        if key in _STRING_FIELDS and not (isinstance(value, str) and value.strip()):
            print(
                f"WARN: dropping field {key!r}={value!r} of record "
                f"{session_id!r}/{model_key!r} — not a non-empty string"
            )
            continue
        if key in _INT_FIELDS and (isinstance(value, bool) or not isinstance(value, int)):
            print(
                f"WARN: dropping field {key!r}={value!r} of record "
                f"{session_id!r}/{model_key!r} — not an int"
            )
            continue
        sanitised[key] = value

    # Beyond a plain Python-type check: the shapes mctl-api's own
    # validateCorrelation rejects at ingest (see orchestrator.usage_ledger
    # TARGET_REPO_RE / DEVLOOP_STAGES / _checked_correlation, which this
    # mirrors so the producer and the collector cannot drift apart).
    stage = sanitised.get("devloop_stage")
    if stage is not None and stage not in DEVLOOP_STAGES:
        print(
            f"WARN: dropping field 'devloop_stage'={stage!r} of record "
            f"{session_id!r}/{model_key!r} — not in the devloop_stage vocabulary"
        )
        del sanitised["devloop_stage"]

    repo = sanitised.get("target_repo")
    if repo is not None and not TARGET_REPO_RE.match(repo):
        print(
            f"WARN: dropping field 'target_repo'={repo!r} of record "
            f"{session_id!r}/{model_key!r} — not owner/name"
        )
        del sanitised["target_repo"]

    for number_key in ("issue_number", "pr_number"):
        if number_key not in sanitised:
            continue
        number = sanitised[number_key]
        if number <= 0:
            print(
                f"WARN: dropping field {number_key!r}={number!r} of record "
                f"{session_id!r}/{model_key!r} — not a positive number"
            )
            del sanitised[number_key]
        elif "target_repo" not in sanitised:
            print(
                f"WARN: dropping field {number_key!r}={number!r} of record "
                f"{session_id!r}/{model_key!r} — no target_repo to read it against"
            )
            del sanitised[number_key]

    return sanitised


def _chunk_records(records: list[dict[str, Any]], max_batch: int, max_body_bytes: int) -> list[list[dict[str, Any]]]:
    """`records` split so no chunk exceeds `max_batch` records or
    `max_body_bytes` of `{"records": [...]}` serialised as JSON — mctl-api's
    own ingest limits (`maxIngestBatch`, `usageMaxBodyBytes`). A single
    record that alone exceeds `max_body_bytes` still gets its own chunk
    rather than being silently dropped; mctl-api's own answer is the final
    word on whether it fits.
    """
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for record in records:
        candidate = [*current, record]
        too_big = len(json.dumps({"records": candidate}).encode("utf-8")) > max_body_bytes
        if current and (len(candidate) > max_batch or too_big):
            chunks.append(current)
            current = [record]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _default_post(url: str, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
    return httpx.post(url, json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)


def deliver(
    records: list[dict[str, Any]],
    *,
    token: str,
    base_url: str = DEFAULT_BASE_URL,
    post: Post | None = None,
    max_batch: int = MAX_BATCH,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> DeliveryResult:
    """POST `records` to `{base_url}{INGEST_PATH}` with the writer bearer,
    chunked at `max_batch` records / `max_body_bytes`.

    Deliberately does not reuse `UsageRecorder._deliver`: that method's
    sticky "may-have-landed" return exists only to decide whether to advance
    the delta baseline, and this caller sends absolute totals read from a
    file — it has no baseline, so a lost answer just means "re-collect next
    tick", nothing needs to be remembered here.

    An empty/absent `token` or a non-`https` `base_url` sends nothing at all
    (one warning each) and is not counted as a failed chunk — this is the
    documented "not configured yet" state, not an error. A chunk that IS
    attempted and fails (transport error or a non-2xx answer) is logged,
    counted in both `undelivered` and `failed_chunks`, and does not stop the
    remaining chunks.
    """
    result = DeliveryResult()
    if not records:
        return result
    if not token.strip():
        print(f"WARN: {TOKEN_ENV} is not set — {len(records)} record(s) not posted")
        result.undelivered = len(records)
        return result
    if not base_url.startswith("https://"):
        print(f"WARN: {BASE_URL_ENV} is not https ({base_url!r}) — {len(records)} record(s) not posted")
        result.undelivered = len(records)
        return result

    poster = post or _default_post
    url = base_url.rstrip("/") + INGEST_PATH
    headers = {"Authorization": f"Bearer {token.strip()}"}
    for chunk in _chunk_records(records, max_batch, max_body_bytes):
        try:
            resp = poster(url, {"records": chunk}, headers)
        except httpx.HTTPError as exc:
            print(
                f"WARN: delivery chunk of {len(chunk)} record(s) failed "
                f"({type(exc).__name__}: {exc}); will retry next tick"
            )
            result.undelivered += len(chunk)
            result.failed_chunks += 1
            continue
        if resp.status_code >= 300:
            print(
                f"WARN: delivery chunk of {len(chunk)} record(s) rejected "
                f"(HTTP {resp.status_code} {resp.text[:300]}); will retry next tick"
            )
            result.undelivered += len(chunk)
            result.failed_chunks += 1
            continue
        try:
            body = resp.json()
        except ValueError:
            body = {}
        accepted = body.get("accepted_count") if isinstance(body, dict) else None
        deduped = body.get("deduped_count") if isinstance(body, dict) else None
        accepted = accepted if isinstance(accepted, int) else 0
        deduped = deduped if isinstance(deduped, int) else 0
        result.accepted += accepted
        result.deduped += deduped
        print(f"OK: delivered chunk of {len(chunk)} record(s) — accepted={accepted} deduped={deduped}")
    return result


def collect(
    *,
    repos: list[str],
    lookback_days: float = DEFAULT_LOOKBACK_DAYS,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
    dry_run: bool = False,
    token: str = "",
    base_url: str = DEFAULT_BASE_URL,
    post: Post | None = None,
    list_fn: Callable[[str, datetime, int], list[Artifact]] = list_artifacts,
    download_fn: Callable[[str, int, int], list[dict[str, Any]] | None] = download_records,
) -> CollectResult:
    """One sweep over `repos`: list, download, sanitise, deliver — delivered
    per repository, not once at the end of the whole sweep.

    A repository whose listing raises, or an artifact whose download raises,
    is logged with its repository/artifact id, counted in `failures`, and
    skipped — the sweep continues with the next repository or artifact. In
    `dry_run`, everything up to delivery still runs; the POST is skipped
    entirely and the count of records that would have been posted is
    reported instead.

    Delivery happens as soon as one repository's artifacts are all read,
    rather than being buffered for a single POST after every repository in
    `repos` has been swept. A wide backfill (`--lookback-days 90` across the
    whole `SERVICES` list) can then make partial, durable progress: if the
    process is interrupted or fails partway through the repository list —
    a rate limit, a timeout, a crash — every repository already swept has
    already been delivered and mctl-api's dedupe makes re-sweeping it on the
    next attempt free, so the sweep converges instead of restarting from
    zero every time.
    """
    result = CollectResult(repositories=len(repos))
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)

    for repo in repos:
        buffer: list[dict[str, Any]] = []
        try:
            artifacts = list_fn(repo, cutoff, max_pages)
        except Exception as exc:  # noqa: BLE001 — one bad repository must not abort the sweep
            print(f"WARN: could not list artifacts for {repo} ({type(exc).__name__}: {exc})")
            result.failures += 1
            continue
        result.artifacts_found += len(artifacts)
        for artifact in artifacts:
            try:
                raw_records = download_fn(repo, artifact.id, max_artifact_bytes)
            except Exception as exc:  # noqa: BLE001 — one bad artifact must not abort the sweep
                print(f"WARN: could not read {repo} artifact {artifact.id} ({type(exc).__name__}: {exc})")
                result.failures += 1
                result.artifacts_skipped += 1
                continue
            if raw_records is None:
                result.artifacts_skipped += 1
                continue
            result.artifacts_read += 1
            for raw in raw_records:
                sanitised = sanitise(raw)
                if sanitised is not None:
                    buffer.append(sanitised)

        if dry_run:
            result.records_posted += len(buffer)
            print(f"[dry-run] would post {len(buffer)} record(s) for {repo}; posting nothing")
        elif buffer:
            delivered = deliver(buffer, token=token, base_url=base_url, post=post)
            result.records_posted += delivered.accepted
            result.records_deduped += delivered.deduped
            result.failures += delivered.failed_chunks

    return result


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Usage-collector — pull reviewer model-usage-records artifacts into the mctl-api usage ledger"
    )
    ap.add_argument(
        "--repo", action="append", dest="repos", default=None, metavar="OWNER/REPO",
        help="Repository to sweep (repeatable). Default: mctlhq/<svc> for svc in config.settings.SERVICES",
    )
    ap.add_argument(
        "--lookback-days", type=float, default=DEFAULT_LOOKBACK_DAYS,
        help=(
            f"Only consider artifacts created within this many days (default: "
            f"{DEFAULT_LOOKBACK_DAYS}). Widening this is a safe backfill — the ledger dedupes."
        ),
    )
    ap.add_argument(
        "--max-pages", type=int, default=DEFAULT_MAX_PAGES,
        help=(
            f"Artifact-listing pages read per repository, {ARTIFACTS_PER_PAGE} artifacts "
            f"each (default: {DEFAULT_MAX_PAGES})"
        ),
    )
    ap.add_argument(
        "--max-artifact-bytes", type=int, default=MAX_ARTIFACT_BYTES,
        help=(
            f"Refuse an artifact zip, or a declared member size, above this many bytes "
            f"(default: {MAX_ARTIFACT_BYTES})"
        ),
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Discover and download as normal; skip the POST. No writer token required.",
    )
    args = ap.parse_args()
    if args.lookback_days <= 0:
        ap.error("--lookback-days must be > 0")
    if args.max_pages <= 0:
        ap.error("--max-pages must be > 0")
    if args.max_artifact_bytes <= 0:
        ap.error("--max-artifact-bytes must be > 0")

    repos = args.repos or [f"mctlhq/{svc}" for svc in SERVICES]

    try:
        _check_gh_auth()
    except (CommandFailed, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"usage-collector: gh could not authenticate — {exc}") from exc

    token = os.environ.get(TOKEN_ENV, "")
    base_url = os.environ.get(BASE_URL_ENV, "").strip() or DEFAULT_BASE_URL

    print(f"Sweeping {len(repos)} repository(ies) for '{ARTIFACT_NAME}' artifacts (lookback={args.lookback_days}d):")
    for repo in repos:
        print(f"  - {repo}")

    result = collect(
        repos=repos,
        lookback_days=args.lookback_days,
        max_pages=args.max_pages,
        max_artifact_bytes=args.max_artifact_bytes,
        dry_run=args.dry_run,
        token=token,
        base_url=base_url,
    )
    print(f"\n=== usage-collector summary === {result.summary()}")
    sys.exit(0)


if __name__ == "__main__":
    main()
