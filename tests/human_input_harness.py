"""Drive the REAL investigator (`investigate()` -> real `_run_agent` -> real
`_seal_draft` -> real publish) with only the outside world faked: the GitHub
issue read, the clone, the issue comment and the SDK client. Shared by the
producer tests and the DevLoopWorkflow end-to-end test (mctlhq/mctl-agents#473).

The "model" is a callable run from the fake SDK client's `query(prompt)`: it
receives the exact prompt the investigator sent and the staging proposal
directory, and writes what a model would (the triplet, maybe a draft)."""
from __future__ import annotations

import dataclasses
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestrator import run_issue_investigator as rii
from tests.conftest import result_message

ISSUE_URL = "https://github.com/mctlhq/mctl-telegram/issues/7"
TRIPLET = ("requirements.md", "design.md", "tasks.md")

Model = Callable[[str, Path], None]


def write_triplet(proposal_dir: Path) -> None:
    for name in TRIPLET:
        (proposal_dir / name).write_text(f"# {name}\n")


class RealInvestigator:
    """`prompts` holds every prompt the real `_run_agent` queried with, and
    `correlations` every value it returned (None when ungranted)."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.correlations: list[Any] = []
        self.model: Model = lambda prompt, proposal_dir: write_triplet(proposal_dir)


def install(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    grant: bool | None,
    author: str = "alice",
    issue_url: str = ISSUE_URL,
) -> RealInvestigator:
    """`grant=True`/`False`: declarative resolver mode with a plan that does
    / does not carry `human.request_input` (forced either way, so the test
    does not depend on what the catalog says today). `grant=None`: legacy
    resolver mode, where there is no plan at all."""
    from orchestrator import options, resolver

    harness = RealInvestigator()
    number = int(issue_url.rsplit("/", 1)[1])
    owner, repo = issue_url.split("/")[3:5]
    issue = rii.IssueData(
        ref=rii.IssueRef(owner=owner, repo=repo, number=number, url=issue_url),
        title="Use library A or library B?",
        body="The issue names two libraries and prefers neither.",
        state="OPEN",
        author=author,
    )
    clone_dir = tmp_path / "clone"
    (clone_dir / "repo").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rii, "gh_issue_view", lambda url: issue)
    monkeypatch.setattr(rii, "_clone_repo", lambda repo, slug: clone_dir)
    monkeypatch.setattr(rii, "post_proposal_comment", lambda *a, **k: None)
    monkeypatch.setattr(rii, "_target_repository_sha", lambda repo_dir: "f" * 40)
    monkeypatch.delenv("ISSUE_INVESTIGATOR_CAPABILITY_MODE", raising=False)

    if grant is None:
        monkeypatch.setenv("ISSUE_INVESTIGATOR_RESOLVER_MODE", "legacy")
    else:
        monkeypatch.setenv("ISSUE_INVESTIGATOR_RESOLVER_MODE", "declarative")
        real_execute = resolver.execute

        def _execute(*args, **kwargs):
            plan = real_execute(*args, **kwargs)
            tools = tuple(t for t in plan.tools if t != options.HUMAN_INPUT_CAPABILITY)
            if grant:
                tools = (*tools, options.HUMAN_INPUT_CAPABILITY)
            return dataclasses.replace(plan, tools=tools)

        monkeypatch.setattr(resolver, "execute", _execute)

    def _options(*args: Any, **kwargs: Any) -> Any:
        # (plan, repo_dir, proposal_dir) or (repo_dir, model, proposal_dir):
        # the proposal dir is the last positional argument of both builders.
        return types.SimpleNamespace(mcp_servers={}, model=None, proposal_dir=args[-1])

    monkeypatch.setattr(options, "build_issue_investigator_options_from_plan", _options)
    monkeypatch.setattr(options, "build_issue_investigator_options", _options)

    class _Client:
        def __init__(self, *, options: Any) -> None:
            self._proposal_dir = Path(options.proposal_dir)

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def query(self, prompt: str) -> None:
            harness.prompts.append(prompt)
            harness.model(prompt, self._proposal_dir)

        async def receive_messages(self):
            yield result_message()

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", _Client)

    real_run_agent = rii._run_agent

    async def _recording_run_agent(*args: Any, **kwargs: Any) -> Any:
        correlation = await real_run_agent(*args, **kwargs)
        harness.correlations.append(correlation)
        return correlation

    monkeypatch.setattr(rii, "_run_agent", _recording_run_agent)
    return harness
