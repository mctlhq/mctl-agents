"""Wiring between service-skill resolution and the runner drivers
(mctlhq/mctl-agents#305): everything between `resolve_bundle` and the prompt
the SDK receives, on the investigator, implementer and declarative-resolver
paths."""
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import resolver, run_implementer, service_skills
from orchestrator import run_issue_investigator as investigator
from orchestrator.service_skills import ServiceSkillPolicy


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _setup_origin(tmp_path: Path) -> Path:
    """A bare 'origin' remote plus a working clone with `origin/HEAD`
    populated — the minimum the merge-base logic needs."""
    upstream = tmp_path / "upstream.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
    work = tmp_path / "work-seed"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "test@test")
    _git(work, "config", "user.name", "test")
    (work / "README.md").write_text("hello\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "remote", "add", "origin", str(upstream))
    _git(work, "push", "-q", "origin", "main")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(upstream), str(clone))
    _git(clone, "config", "user.email", "test@test")
    _git(clone, "config", "user.name", "test")
    _git(clone, "remote", "set-head", "origin", "main")
    return clone


def _write_skills_manifest(repo: Path) -> None:
    root = repo / ".mctl" / "skills"
    (root / "a").mkdir(parents=True, exist_ok=True)
    (root / "manifest.yaml").write_text(
        "apiVersion: agents.mctl.ai/v1alpha1\n"
        "kind: ServiceSkillSet\n"
        "metadata: {service: test-service}\n"
        "spec:\n"
        "  bindings:\n"
        "    implementer: [a]\n"
        "  skills:\n"
        "    a: {path: .mctl/skills/a/SKILL.md}\n"
    )
    (root / "a" / "SKILL.md").write_text(
        "---\nname: a\ndescription: d\n---\n\n# a\n\nBody.\n"
    )


# ---------------------------------------------------------------------------
# Investigator prompt splice
# ---------------------------------------------------------------------------
def test_splice_lands_between_context_and_what_to_produce():
    prompt = "intro\n## Your working context\nstuff\n## What to produce\ntail\n"
    spliced = investigator._splice_service_skills(prompt, "<service_skills>BLOCK</service_skills>")
    ctx = spliced.index("## Your working context")
    block = spliced.index("BLOCK")
    produce = spliced.index("## What to produce")
    assert ctx < block < produce
    assert spliced.count("BLOCK") == 1


def test_splice_raises_on_drifted_template():
    with pytest.raises(RuntimeError, match="splice anchor"):
        investigator._splice_service_skills("no anchor here", "BLOCK")


def test_investigator_build_prompt_contains_block_and_anchor():
    """The real template still carries the anchor, and a non-empty block
    lands inside the rendered prompt exactly once."""
    ref = investigator.IssueRef(
        owner="mctlhq", repo="x", number=1, url="https://github.com/mctlhq/x/issues/1"
    )
    issue = investigator.IssueData(ref=ref, title="t", body="b", state="OPEN")
    prompt = investigator._build_prompt(
        issue, "x", "slug", service_skills_block="<service_skills>WIRED</service_skills>"
    )
    assert prompt.count("WIRED") == 1
    assert prompt.index("WIRED") < prompt.index("## What to produce")


def test_prompt_block_legacy_mode_never_touches_the_clone(monkeypatch):
    """R23: `legacy` mode returns "" without reading the target repository —
    proven by handing it a path that does not exist."""
    monkeypatch.delenv("ISSUE_INVESTIGATOR_RESOLVER_MODE", raising=False)
    assert investigator._service_skills_prompt_block(Path("/nonexistent/never-read")) == ""


# ---------------------------------------------------------------------------
# Implementer resolution and prompt
# ---------------------------------------------------------------------------
def test_implementer_resolution_disabled_policy_skips_pin_sha(monkeypatch):
    """R17 ordering: a disabled policy resolves an empty bundle WITHOUT
    invoking pin_sha (no git subprocess). The manifest is stubbed so the
    premise is this test's own, not the repository's current state."""
    stub = SimpleNamespace(service_skills=ServiceSkillPolicy(enabled=False), tool_allow=())
    monkeypatch.setattr(run_implementer, "load_agent_manifest", lambda _path: stub)

    def _boom(*a, **k):
        raise AssertionError("pin_sha must not run for a disabled policy")

    monkeypatch.setattr(run_implementer, "service_skill_pin_sha", _boom)
    bundle = run_implementer._resolve_implementer_service_skills(Path("/nonexistent"), "feat/agents-x")
    assert bundle.skills == ()


def test_implementer_build_prompt_carries_block():
    ref = run_implementer.ProposalRef(
        service="x", slug="s", proposal_dir=Path("/tmp/p"), status="accepted"
    )
    prompt = run_implementer._build_prompt(
        ref, service_skills_block="<service_skills>WIRED</service_skills>"
    )
    assert prompt.count("WIRED") == 1


# ---------------------------------------------------------------------------
# Declarative path: R6 pin derivation in _resolve_service_skill_bundle_for_plan
# ---------------------------------------------------------------------------
def test_declarative_path_pins_agent_authored_bundle_to_merge_base(tmp_path):
    """A skills edit committed on the work branch must not be readable by
    the next run: the resolver derives the merge-base pin itself, while
    `Task.target_repository_sha` keeps carrying branch HEAD (honest plan
    provenance)."""
    clone = _setup_origin(tmp_path)
    base_sha = _head(clone)

    _git(clone, "checkout", "-q", "-b", "fix/adopted-branch")
    _write_skills_manifest(clone)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "previous run committed a skill on the branch")
    branch_head = _head(clone)
    assert branch_head != base_sha

    profile = SimpleNamespace(service_skills=ServiceSkillPolicy(enabled=True), tools=())
    task = resolver.Task(target_repository_sha=branch_head, target_repo_dir=clone)
    bundle = resolver._resolve_service_skill_bundle_for_plan(
        agent="implementer", profile=profile, task=task
    )
    assert bundle.resolved_from_sha == base_sha
    assert bundle.skills == ()
    # The task's own field is untouched -- it still records what the agent
    # actually runs against.
    assert task.target_repository_sha == branch_head


def test_declarative_path_head_pin_for_read_only_agent(tmp_path):
    clone = _setup_origin(tmp_path)
    _write_skills_manifest(clone)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "skills on main")
    head = _head(clone)

    policy = ServiceSkillPolicy(enabled=True)
    profile = SimpleNamespace(service_skills=policy, tools=())
    task = resolver.Task(target_repository_sha=head, target_repo_dir=clone)
    bundle = resolver._resolve_service_skill_bundle_for_plan(
        agent="issue-investigator", profile=profile, task=task
    )
    assert bundle.resolved_from_sha == head
    # bindings name only `implementer`, so the read-only agent's bundle is
    # empty -- the point here is the pin, not the selection.
    assert bundle.skills == ()


def test_declarative_path_kill_switch_runs_no_git(tmp_path, monkeypatch):
    """R25: with MCTL_SERVICE_SKILLS=off the declarative path must not run
    a single git subprocess, even for an agent-authored agent -- proven by
    making the merge-base helper explode if called."""
    monkeypatch.setenv("MCTL_SERVICE_SKILLS", "off")

    def _boom(*a, **k):
        raise AssertionError("kill switch engaged: no git may run")

    monkeypatch.setattr(service_skills, "_merge_base_with_default_branch", _boom)
    profile = SimpleNamespace(service_skills=ServiceSkillPolicy(enabled=True), tools=())
    task = resolver.Task(
        target_repository_sha="deadbeef", target_repo_dir=Path("/nonexistent")
    )
    bundle = resolver._resolve_service_skill_bundle_for_plan(
        agent="implementer", profile=profile, task=task
    )
    assert bundle.skills == ()


def test_agent_authored_constant_is_what_wiring_depends_on():
    assert set(service_skills.AGENT_AUTHORED_AGENTS) == {"implementer", "shepherd"}
