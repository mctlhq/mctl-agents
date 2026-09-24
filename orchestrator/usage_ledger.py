"""Model-usage producer: one mctl-api usage record per model per turn
(mctlhq/.github#50, ADR-012).

Where it runs. `tracing.agent_run` hands every SDK message to
`UsageRecorder.observe`, so the investigator, the implementer and the
shepherd record usage from the one place that already sees their whole
stream, whether tracing is on or off. Helpers that only drain a stream
another run owns (`subagent_wait`, `mcp_guard`) feed the same observer and
never record on their own, so a delegated sub-agent is not counted twice.

The credential. Records go to `POST /api/v1/usage/records` with
`MCTL_USAGE_WRITER_TOKEN`, the bearer of `service:mctl-agents-usage`, which
may append usage records and do nothing else. The admin `MCTL_TOKEN` is never
used here: a producer that fell back to it would put ingestion back on the
admin principal, the exact thing variant B of #50 rules out. No token, no
records, one warning.

Deltas, not totals. `ResultMessage.model_usage` is CUMULATIVE for the SDK
session: measured on claude-agent-sdk 0.2.136, the second turn of one
`ClaudeSDKClient` session reported output 100 after 53, i.e. 53 + 47. A
session that ends several turns (the implementer and the investigator drain
past the first result, mctl-agents#366) would be counted once per turn if
each ResultMessage were recorded as-is. So each record carries what THIS
turn added for that model: its cumulative counters minus the ones last seen
for the same (session, model). The rows of a session sum to its total.

Idempotency. mctl-api derives the row id from (session_id, result_uuid,
model_key) and ignores a second insert of it, so a re-sent batch counts once.
In the process, the same key is remembered too: observing one ResultMessage
twice sends it once and, more importantly, does not advance the delta
baseline a second time.

Failed deliveries. The baseline advances only once a batch may have landed.
A batch that certainly did not (no connection, or an HTTP error answer: the
ingest is one transaction, so nothing of it was stored) leaves the baseline
where it was, and the next turn's delta carries that usage instead of losing
it. A batch that may have been stored (the answer was lost) advances it:
counting a turn twice is the worse error for a ledger.

Off the event loop. `observe` is called from inside the drivers' async
message loops, so it only queues: one daemon thread per process plans,
delivers and commits, in order, and is the only writer of a recorder's
state. A blocking POST there would stall the loop and push back every anyio
deadline the drivers rely on (`fail_after`, the #366 drain). Whatever is
still queued when the interpreter exits is flushed by an `atexit` hook,
bounded by FLUSH_TIMEOUT_SECONDS; by then the anyio loop has returned.

Never fatal. Recording is bookkeeping about a run, not part of it: every
failure here is logged and swallowed.
"""
from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TOKEN_ENV = "MCTL_USAGE_WRITER_TOKEN"  # noqa: S105 — an env var name, not a credential
BASE_URL_ENV = "MCTL_API_BASE_URL"
DEFAULT_BASE_URL = "https://api.mctl.ai"
INGEST_PATH = "/api/v1/usage/records"
SCHEMA_VERSION = 1

# Two attempts at 5 s on the delivery thread; the ingest is idempotent, so a
# retry after a lost answer is safe. The exit flush allows about two full
# batches of that.
REQUEST_TIMEOUT_SECONDS = 5.0
ATTEMPTS = 2
RETRY_DELAY_SECONDS = 1.0
FLUSH_TIMEOUT_SECONDS = 25.0

# Failures that mean the request never reached mctl-api, so nothing of it
# can have been stored. Any other failure may have been (the answer was
# lost), and is treated as such.
_UNSENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.WriteError,
    httpx.WriteTimeout,
)

# The ADR-012 agent vocabulary. `tracing.agent_run` names are kept as they
# are for spans; the ledger speaks the shorter names.
_AGENT_NAMES = {"issue-investigator": "investigator"}

# ModelUsage counter -> record field. All cumulative within a session.
_COUNTERS = (
    ("inputTokens", "input_tokens"),
    ("outputTokens", "output_tokens"),
    ("cacheReadInputTokens", "cache_read_tokens"),
    ("cacheCreationInputTokens", "cache_write_tokens"),
    ("webSearchRequests", "web_search_requests"),
)

# Correlation read from the runner pod's environment (the CWFTs set these).
_CORRELATION_ENV = (
    ("WORKFLOW_TEMPORAL_WORKFLOW_ID", "temporal_workflow_id"),
    ("WORKFLOW_NAME", "argo_workflow_name"),
    ("WORKFLOW_WORK_ITEM_ID", "work_item_id"),
)

Post = Callable[[str, dict[str, Any], dict[str, str]], httpx.Response]


def agent_env_without_writer_token(env: Mapping[str, str]) -> dict[str, str]:
    """`env` with the usage-writer token blanked, for an SDK session's env.

    The SDK layers `ClaudeAgentOptions.env` over the inherited environment,
    so blanking the key here is what keeps the token out of the CLI child and
    everything the model runs through Bash. Only this module's own process
    needs it.
    """
    return {**env, TOKEN_ENV: ""}


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _default_post(url: str, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
    return httpx.post(url, json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)


class _Worker:
    """The one delivery thread of this process: jobs run in order."""

    def __init__(self) -> None:
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def submit(self, job: Callable[[], None]) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="usage-ledger", daemon=True)
                self._thread.start()
        self._queue.put(job)

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                job()
            except Exception:  # a job must not kill the thread that runs the next one
                logger.exception("usage ledger job failed")
            finally:
                self._queue.task_done()

    def flush(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True


_WORKER = _Worker()


def flush(timeout: float = FLUSH_TIMEOUT_SECONDS) -> bool:
    """Wait for the queued usage records to be delivered; False on timeout.

    Registered with `atexit`, so a runner that exits right after its last
    turn still delivers that turn.
    """
    done = _WORKER.flush(timeout)
    if not done:
        logger.warning("usage ledger: stopped waiting after %.0fs; queued usage records were not delivered", timeout)
    return done


atexit.register(flush)


class UsageRecorder:
    """Builds and delivers the usage records of one agent's SDK sessions."""

    def __init__(
        self,
        agent: str,
        *,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        correlation: Mapping[str, Any] | None = None,
        post: Post | None = None,
        sleep: Callable[[float], None] = time.sleep,
        submit: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.agent = _AGENT_NAMES.get(agent, agent)
        self._token = token.strip()
        self._off_reason = "" if self._token else f"{TOKEN_ENV} is not set"
        if self._token and not base_url.startswith("https://"):
            # Operator-supplied, like everywhere else this variable is read
            # (run_shepherd, publish_agent_release): never send the bearer
            # over anything but https.
            self._token = ""
            self._off_reason = f"{BASE_URL_ENV} is not https ({base_url!r})"
        self._url = base_url.rstrip("/") + INGEST_PATH
        self._correlation = {k: v for k, v in (correlation or {}).items() if v not in (None, "")}
        self._post = post or _default_post
        self._sleep = sleep
        self._submit = submit or _WORKER.submit
        self._undelivered = 0
        # (session_id, result_uuid or turn marker): results already handled.
        # A result is committed whole, so the model is not part of it.
        self._seen: set[tuple[str, str]] = set()
        # (session_id, model_key) -> the cumulative counters last recorded.
        self._baseline: dict[tuple[str, str], dict[str, int]] = {}
        self._warned: set[str] = set()

    @classmethod
    def from_env(
        cls,
        agent: str,
        environ: Mapping[str, str] | None = None,
        **correlation: Any,
    ) -> UsageRecorder:
        env = os.environ if environ is None else environ
        found = {field: env.get(name, "").strip() for name, field in _CORRELATION_ENV}
        return cls(
            agent,
            token=env.get(TOKEN_ENV, ""),
            base_url=env.get(BASE_URL_ENV, "").strip() or DEFAULT_BASE_URL,
            correlation={**found, **correlation},
        )

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(message, *args)

    def observe(self, message: Any) -> None:
        """Queue `message` for recording if it is a ResultMessage.

        Returns at once and never raises: the work happens on the delivery
        thread.
        """
        if type(message).__name__ != "ResultMessage":
            return
        if not self.enabled:
            self._warn_once(
                "disabled", "usage recording is off (%s): this run records no model usage", self._off_reason
            )
            return
        try:
            self._submit(lambda: self._record(message))
        except Exception as exc:  # noqa: BLE001 — recording must never break the run it records
            self._warn_once("submit", "could not queue model usage (%s: %s)", type(exc).__name__, exc)

    def _record(self, message: Any) -> None:
        try:
            planned = self._plan(message)
            if planned and self._deliver([record for _, _, _, record in planned]):
                self._commit(planned)
        except Exception as exc:  # noqa: BLE001 — recording must never break the run it records
            self._warn_once("observe", "could not record model usage (%s: %s)", type(exc).__name__, exc)

    def records_for(self, message: Any) -> list[dict[str, Any]]:
        """The records one ResultMessage adds, without delivering them.

        Empty for a message already handled, and for one with no session or
        no per-model usage (nothing it reports can be attributed). Pure: it
        neither delivers nor marks anything as handled.
        """
        return [record for _, _, _, record in self._plan(message)]

    def _commit(self, planned: list[tuple[Any, Any, dict[str, int], dict[str, Any]]]) -> None:
        for seen_key, baseline_key, cumulative, _ in planned:
            self._seen.add(seen_key)
            self._baseline[baseline_key] = cumulative

    def _plan(self, message: Any) -> list[tuple[Any, Any, dict[str, int], dict[str, Any]]]:
        session_id = str(getattr(message, "session_id", "") or "").strip()
        model_usage = getattr(message, "model_usage", None)
        if not session_id or not isinstance(model_usage, Mapping) or not model_usage:
            return []
        result_uuid = str(getattr(message, "uuid", "") or "").strip()
        num_turns = _int(getattr(message, "num_turns", None))
        # Without a uuid the server keys on num_turns, so the local key does
        # too: the two must agree on what "the same result" means.
        turn_key = result_uuid or f"turns:{num_turns}"
        seen_key = (session_id, turn_key)
        if seen_key in self._seen:
            return []
        outcome = "error" if getattr(message, "is_error", False) else "success"
        api_error_status = getattr(message, "api_error_status", None)
        common: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "agent": self.agent,
            "outcome": outcome,
            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            **self._correlation,
        }
        if result_uuid:
            common["result_uuid"] = result_uuid
        if num_turns is not None:
            common["num_turns"] = num_turns
        duration_api_ms = _int(getattr(message, "duration_api_ms", None))
        if duration_api_ms is not None:
            common["duration_api_ms"] = duration_api_ms
        if api_error_status not in (None, ""):
            common["api_error_status"] = str(api_error_status)
        for attr in ("stop_reason", "terminal_reason"):
            value = getattr(message, attr, None)
            if isinstance(value, str) and value:
                common[attr] = value

        planned: list[tuple[Any, Any, dict[str, int], dict[str, Any]]] = []
        for model_key, usage in model_usage.items():
            if not isinstance(usage, Mapping):
                continue
            current = {field: n for source, field in _COUNTERS if (n := _int(usage.get(source))) is not None}
            previous = self._baseline.get((session_id, str(model_key)), {})
            delta: dict[str, int] = {}
            for field, value in current.items():
                step = value - previous.get(field, 0)
                if step < 0:
                    # A counter went backwards: this is not the cumulative
                    # series it was assumed to be. Take the value as this
                    # turn's own rather than record a negative.
                    self._warn_once(
                        "not-cumulative",
                        "model_usage for %s went backwards (%s %d -> %d); recording the reported value",
                        model_key, field, previous.get(field, 0), value,
                    )
                    step = value
                delta[field] = step
            record = {**common, "model_key": str(model_key), **delta}
            canonical = usage.get("canonicalModel")
            if isinstance(canonical, str) and canonical:
                record["canonical_model"] = canonical
            provider = usage.get("provider")
            if isinstance(provider, str) and provider:
                record["provider"] = provider
            planned.append((seen_key, (session_id, str(model_key)), {**previous, **current}, record))
        return planned

    def _deliver(self, records: list[dict[str, Any]]) -> bool:
        """POST `records`; True when they may have been stored.

        "May have been stored" is sticky across attempts: once one attempt
        lost its answer, a later attempt that plainly failed does not make
        the batch certainly-unstored, and carrying its tokens into the next
        turn (under another result_uuid, which no server dedupe catches)
        would count them twice.
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        body = {"records": records}
        may_have_landed = False
        failure = ""
        for attempt in range(1, ATTEMPTS + 1):
            try:
                resp = self._post(self._url, body, headers)
            except httpx.HTTPError as exc:
                if not isinstance(exc, _UNSENT):
                    may_have_landed = True
                failure = type(exc).__name__
                if attempt < ATTEMPTS:
                    self._sleep(RETRY_DELAY_SECONDS)
                    continue
                break
            if resp.status_code < 300:
                return True
            # An HTTP error answer: the ingest is one transaction, so this
            # attempt stored nothing.
            failure = f"HTTP {resp.status_code} {resp.text[:300]}"
            if (resp.status_code >= 500 or resp.status_code == 429) and attempt < ATTEMPTS:
                self._sleep(RETRY_DELAY_SECONDS)
                continue
            break
        # Every failure is logged, with the running total: a run whose
        # deliveries keep failing must not go quiet after the first one.
        self._undelivered += len(records)
        logger.warning(
            "usage records not delivered (%s); %d record(s) undelivered so far in this process",
            failure, self._undelivered,
        )
        return may_have_landed
