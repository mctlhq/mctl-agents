"""Scoped run token for an agent pod's mctl MCP connection (mctlhq/mctl-agents#737).

The pod trades its projected ServiceAccount token for an mctl-api run token
(`art_...`) at `POST /api/v1/agent-run-tokens/exchange`. mctl-api decides
everything the token may do from the pod's own Workflow (agent, scope,
execution) -- this module sends no claims, only the Kubernetes token.

`MCTL_RUN_TOKEN_MODE` picks the stage:

- `off` (default, also unset): nothing changes; MCP uses MCTL_TOKEN.
- `observe`: the exchange runs and its token is revoked at once, proving the
  path end to end; MCP stays on MCTL_TOKEN and names the scope in
  `X-Mctl-Agent-Scope-Shadow`, so mctl-api counts what the scope would have
  refused without refusing anything. A failed exchange is logged, never
  fatal.
- `enforce`: MCP uses the run token. A failed exchange fails the run: a run
  that was meant to be scoped never silently falls back to the static
  credential.

Stage one covers the MCP connection only; the pod's own REST clients still
use MCTL_TOKEN.

The run token is held in this module, not in the environment, so it does
not reach the agent's subprocesses.
"""

from __future__ import annotations

import atexit
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from config.settings import MCTL_MCP_URL

MODE_ENV = "MCTL_RUN_TOKEN_MODE"
SA_TOKEN_FILE_ENV = "MCTL_RUN_TOKEN_SA_TOKEN_FILE"  # noqa: S105 — an env var name, not a credential
DEFAULT_SA_TOKEN_FILE = "/var/run/secrets/mctl-api/token"  # noqa: S105 — a file path, not a credential
SHADOW_HEADER = "X-Mctl-Agent-Scope-Shadow"

MODE_OFF = "off"
MODE_OBSERVE = "observe"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_OBSERVE, MODE_ENFORCE)

EXCHANGE_PATH = "/api/v1/agent-run-tokens/exchange"
REVOKE_PATH = "/api/v1/agent-run-tokens/self/revoke"
REQUEST_TIMEOUT_SECONDS = 15.0


class RunTokenError(RuntimeError):
    """The run token a run in enforce mode needs could not be obtained."""


@dataclass(frozen=True)
class RunToken:
    token: str
    token_id: str
    scope: str
    execution_id: str
    expires_at: str


@dataclass
class _State:
    mode: str = MODE_OFF
    scope: str = ""
    token: RunToken | None = None


_state = _State()


def mode() -> str:
    raw = os.environ.get(MODE_ENV, "").strip().lower() or MODE_OFF
    if raw not in MODES:
        # A typo must not quietly mean "off" for a run meant to be scoped.
        raise RunTokenError(f"{MODE_ENV}={raw!r} is not one of {', '.join(MODES)}")
    return raw


def _api_base() -> str:
    base = MCTL_MCP_URL.rstrip("/")
    return base[: -len("/mcp")] if base.endswith("/mcp") else base


def _post(url: str, bearer: str) -> httpx.Response:
    return httpx.post(url, headers={"Authorization": f"Bearer {bearer}"}, timeout=REQUEST_TIMEOUT_SECONDS)


def exchange(sa_token_file: str | None = None, post=_post) -> RunToken:
    """Exchange the pod's projected ServiceAccount token for a run token."""
    path = Path(sa_token_file or os.environ.get(SA_TOKEN_FILE_ENV, "").strip() or DEFAULT_SA_TOKEN_FILE)
    try:
        sa_token = path.read_text().strip()
    except OSError as exc:
        raise RunTokenError(f"no projected ServiceAccount token at {path}: {exc}") from exc
    if not sa_token:
        raise RunTokenError(f"projected ServiceAccount token at {path} is empty")
    try:
        resp = post(_api_base() + EXCHANGE_PATH, sa_token)
    except httpx.HTTPError as exc:
        raise RunTokenError(f"run token exchange failed: {exc}") from exc
    if resp.status_code != 201:
        code = ""
        try:
            err = resp.json()
        except ValueError:
            err = None
        # Whatever answered (a proxy, a gateway) may send any JSON or none.
        if isinstance(err, dict) and isinstance(err.get("code"), str):
            code = err["code"]
        raise RunTokenError(f"run token exchange refused: HTTP {resp.status_code} {code}".rstrip())
    # A malformed success body is a failed exchange like any other, so it
    # stays inside RunTokenError and observe mode's catch.
    try:
        body = resp.json()
    except ValueError as exc:
        raise RunTokenError(f"run token exchange answered with invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise RunTokenError("run token exchange answered with a non-object body")

    def field(name: str) -> str:
        value = body.get(name)
        return value if isinstance(value, str) else ""

    token = field("token")
    if not token.startswith("art_"):
        raise RunTokenError("run token exchange answered without a run token")
    return RunToken(
        token=token,
        token_id=field("token_id"),
        scope=field("scope"),
        execution_id=field("execution_id"),
        expires_at=field("expires_at"),
    )


def revoke(token: RunToken, post=_post) -> None:
    """Revoke a run token with itself. Never raises: an unrevoked token still
    ends with its run, by expiry or its execution turning terminal."""
    try:
        resp = post(_api_base() + REVOKE_PATH, token.token)
        if resp.status_code != 204:
            print(f"warn: run token {token.token_id} revoke answered HTTP {resp.status_code}")
    except httpx.HTTPError as exc:
        print(f"warn: run token {token.token_id} revoke failed: {exc}")


def setup(shadow_scope: str, post=_post) -> None:
    """Acquire this run's token per MCTL_RUN_TOKEN_MODE. Call once, before the
    agent's options are built. shadow_scope is the scope observe mode names
    when the exchange itself failed; otherwise mctl-api's answer is used."""
    m = mode()
    _state.mode, _state.scope, _state.token = m, "", None
    if m == MODE_OFF:
        return
    if m == MODE_ENFORCE:
        tok = exchange(post=post)
        _state.token, _state.scope = tok, tok.scope
        atexit.register(revoke, tok, post)
        print(f"run token: enforce, scope={tok.scope} token_id={tok.token_id} expires_at={tok.expires_at}")
        return
    try:
        tok = exchange(post=post)
    except RunTokenError as exc:
        print(f"warn: run token observe: {exc}")
        _state.scope = shadow_scope
        return
    _state.scope = tok.scope or shadow_scope
    print(f"run token: observe, exchange ok, scope={tok.scope} token_id={tok.token_id}; revoking it")
    revoke(tok, post)


def mcp_auth(static_token: str) -> tuple[str, dict[str, str]]:
    """The bearer and extra headers for the mctl MCP connection: the run
    token in enforce mode, otherwise the static token, plus the shadow scope
    header in observe mode."""
    # The environment decides enforce, not only setup(): a run configured
    # for enforce that never called setup() must not connect with the static
    # token.
    if _state.mode == MODE_ENFORCE or mode() == MODE_ENFORCE:
        if _state.token is None:
            raise RunTokenError("enforce mode without a run token: call run_token.setup() first")
        return _state.token.token, {}
    if _state.mode == MODE_OBSERVE and _state.scope and static_token:
        return static_token, {SHADOW_HEADER: _state.scope}
    return static_token, {}


def _reset_for_tests() -> None:
    _state.mode, _state.scope, _state.token = MODE_OFF, "", None
