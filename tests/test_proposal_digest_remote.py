"""The proposal content digest read from GitHub (mctlhq/mctl-agents#681).

`read_proposal_content_digest` is what the DevLoop binds into the approval
request before the human is asked. It must equal the local
`proposal_content_digest` of the same bytes (the implementer and the approve
CWFT compute that one), read every file at one resolved commit, and treat
only GitHub's 404 as "not there": any other unreadable answer raises.
"""
from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from orchestrator.proposal_state import PROPOSAL_DIGEST_FILES, proposal_content_digest
from orchestrator.temporal.activities import proposals
from orchestrator.temporal.activities.proposals import ProposalListingError, read_proposal_content_digest

pytestmark = pytest.mark.anyio

SHA = "0123456789abcdef0123456789abcdef01234567"
FILES = {"requirements.md": b"# R\n\xc3\xa9 \r\n", "design.md": b"", "tasks.md": b"- [ ] one\n"}
PREFIX = "/repos/mctlhq/mctl-gitops/contents/platform-gitops/agents-state/mctl-agents/proposals/issue-7-x/"


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")


class _GitHub:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.refs: list[str] = []
        self.override: dict[str, httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/mctlhq/mctl-gitops/commits/main":
            return self.override.get("head", httpx.Response(200, json={"sha": SHA}))
        name = path.removeprefix(PREFIX)
        self.refs.append(request.url.params.get("ref", ""))
        if name in self.override:
            return self.override[name]
        if name not in self.files:
            return httpx.Response(404, json={"message": "Not Found"})
        content = base64.encodebytes(self.files[name]).decode()
        return httpx.Response(200, json={"type": "file", "encoding": "base64", "content": content})


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> _GitHub:
    fake = _GitHub(FILES)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        return real(*args, **kwargs)

    monkeypatch.setattr(proposals.httpx, "AsyncClient", factory)
    return fake


def _local(tmp_path: Path, files: dict[str, bytes]) -> str:
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
    return proposal_content_digest(tmp_path)


async def test_the_remote_digest_is_the_local_digest_of_the_same_bytes(github, tmp_path):
    digest, commit = await read_proposal_content_digest("mctl-agents", "issue-7-x")

    assert digest == _local(tmp_path, FILES) and commit == SHA


async def test_every_file_is_read_at_the_one_resolved_commit(github):
    await read_proposal_content_digest("mctl-agents", "issue-7-x")

    assert github.refs == [SHA] * len(PROPOSAL_DIGEST_FILES)


async def test_changed_content_is_a_different_digest(github):
    before, _ = await read_proposal_content_digest("mctl-agents", "issue-7-x")
    github.files["tasks.md"] += b"- [ ] two\n"

    after, _ = await read_proposal_content_digest("mctl-agents", "issue-7-x")

    assert before != after


@pytest.mark.parametrize(
    ("where", "response"),
    [
        ("tasks.md", httpx.Response(500)),
        ("tasks.md", httpx.Response(403, json={"message": "rate limited"})),
        ("tasks.md", httpx.Response(200, json=[{"name": "nested"}])),
        ("tasks.md", httpx.Response(200, json={"type": "symlink", "target": "../x"})),
        ("tasks.md", httpx.Response(200, json={"type": "file", "encoding": "none", "content": ""})),
        ("tasks.md", httpx.Response(200, text="not json")),
        ("head", httpx.Response(404)),
        ("head", httpx.Response(200, json={"sha": "short"})),
    ],
)
async def test_an_unreadable_answer_raises_and_is_never_hashed_as_absent(github, where, response):
    github.override[where] = response

    with pytest.raises(ProposalListingError):
        await read_proposal_content_digest("mctl-agents", "issue-7-x")


async def test_a_transport_failure_raises(monkeypatch):
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        proposals.httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(broken)})
    )

    with pytest.raises(ProposalListingError):
        await read_proposal_content_digest("mctl-agents", "issue-7-x")


async def test_no_token_is_refused_not_read_anonymously(github, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN")

    with pytest.raises(ProposalListingError):
        await read_proposal_content_digest("mctl-agents", "issue-7-x")
