"""Tests for tools/publish_agent_release.py.

This script runs unattended on every release, with a token that can write
the agent registry, and until now had no automated gate at all (claude P2
on #238). The two things worth pinning down are the ones a silent bug
would be invisible in: which files a manifest glob actually selects, and
whether one bad manifest can take the rest of the release down with it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "publish_agent_release.py"
_spec = importlib.util.spec_from_file_location("publish_agent_release", _TOOL)
assert _spec and _spec.loader
publish_agent_release = importlib.util.module_from_spec(_spec)
sys.modules["publish_agent_release"] = publish_agent_release
_spec.loader.exec_module(publish_agent_release)

_glob_match = publish_agent_release._glob_match


class TestGlobMatch:
    """Shell semantics, not fnmatch's: `*` must not cross a slash."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("agents/mctl-api/CLAUDE.md", True),
            # The regression: fnmatch's `*` crosses `/`, so this nested
            # file matched too and its edits moved the prompt_hash of an
            # agent that never declared it.
            ("agents/mctl-api/nested/CLAUDE.md", False),
            # `[!_]` still excludes the shared _manifests/_generic dirs.
            ("agents/_generic/CLAUDE.md", False),
            ("agents/mctl-api/CLAUDE.md.bak", False),
        ],
    )
    def test_single_star_stays_within_one_segment(self, path, expected):
        assert _glob_match(path, "agents/[!_]*/CLAUDE.md") is expected

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("agents/mctl-api/context/a.md", True),
            # `**` is the one wildcard that may span segments — the
            # manifests use it precisely to pull a whole tree in.
            ("agents/mctl-api/context/deep/nested/b.md", True),
            ("agents/_generic/context/a.md", False),
            ("agents/mctl-api/other/a.md", False),
        ],
    )
    def test_double_star_spans_segments(self, path, expected):
        assert _glob_match(path, "agents/[!_]*/context/**") is expected

    def test_question_mark_is_a_single_non_slash_character(self):
        assert _glob_match("agents/a/x.md", "agents/?/x.md") is True
        assert _glob_match("agents/ab/x.md", "agents/?/x.md") is False

    def test_a_dot_is_literal_not_any_character(self):
        """A regex translation that forgot to escape would match this."""
        assert _glob_match("agents/x/CLAUDEXmd", "agents/[!_]*/CLAUDE.md") is False

    def test_the_repo_manifests_still_select_their_own_agent_dirs(self):
        """Guards the real patterns, not just synthetic ones: the shipped
        manifests must keep matching the files they are written for."""
        tree = [
            "agents/mctl-api/CLAUDE.md",
            "agents/mctl-api/.claude/agents/implementer.md",
            "agents/_manifests/implementer/agent.yaml",
        ]
        assert [p for p in tree if _glob_match(p, "agents/[!_]*/.claude/agents/implementer.md")] == [
            "agents/mctl-api/.claude/agents/implementer.md"
        ]


class TestPromptHash:
    def _manifest(self, sources):
        return {"spec": {"prompt": {"sources": sources}}}

    def test_hashes_every_file_a_glob_matched(self, monkeypatch):
        reads: list[str] = []

        def fake_read(tag: str, relpath: str) -> bytes | None:
            reads.append(relpath)
            return b"content-of-" + relpath.encode()

        monkeypatch.setattr(publish_agent_release, "_read_at_tag", fake_read)
        tree = ["agents/a/CLAUDE.md", "agents/b/CLAUDE.md", "agents/a/nested/CLAUDE.md"]
        digest = publish_agent_release.prompt_hash(
            self._manifest([{"glob": "agents/[!_]*/CLAUDE.md"}]), "x", "1.0.0", tree
        )
        assert digest.startswith("sha256:")
        # The nested file is NOT part of the surface, so editing it must
        # not move the hash.
        assert sorted(reads) == ["agents/a/CLAUDE.md", "agents/b/CLAUDE.md"]

    def test_a_missing_source_is_fatal_not_a_short_hash(self, monkeypatch):
        monkeypatch.setattr(publish_agent_release, "_read_at_tag", lambda tag, relpath: None)
        with pytest.raises(publish_agent_release.PublishError) as caught:
            publish_agent_release.prompt_hash(
                self._manifest([{"inline": "orchestrator/gone.py:build"}]), "x", "1.0.0", []
            )
        assert "not in 1.0.0's tree" in str(caught.value)

    def test_a_glob_matching_nothing_still_contributes_the_pattern(self, monkeypatch):
        """Otherwise the hash would not change when the first file appears."""
        monkeypatch.setattr(publish_agent_release, "_read_at_tag", lambda tag, relpath: b"x")
        empty = publish_agent_release.prompt_hash(
            self._manifest([{"glob": "agents/[!_]*/none.md"}]), "x", "1.0.0", []
        )
        other = publish_agent_release.prompt_hash(
            self._manifest([{"glob": "agents/[!_]*/different.md"}]), "x", "1.0.0", []
        )
        assert empty != other

    def test_a_manifest_without_sources_is_refused(self):
        with pytest.raises(publish_agent_release.PublishError):
            publish_agent_release.prompt_hash(self._manifest([]), "x", "1.0.0", [])


class TestPerAgentIsolation:
    """agy P2 on #238: one bad manifest must not abandon the rest.

    The failure mode this replaced was worse than a failed release — it
    left the registry half-published, in a state no single re-run
    reproduces, with the pins it did write looking perfectly current.
    """

    def _run(self, monkeypatch, capsys, published: list[str], explode: dict[str, Exception]):
        monkeypatch.setattr(publish_agent_release, "_git", lambda *a: "cafe1234cafe")
        monkeypatch.setattr(
            publish_agent_release,
            "_tree_paths",
            lambda tag: [
                "agents/_manifests/alpha/agent.yaml",
                "agents/_manifests/beta/agent.yaml",
                "agents/_manifests/gamma/agent.yaml",
            ],
        )

        def fake_publish(agent, version, git_sha, tree, *, dry_run):
            if agent in explode:
                raise explode[agent]
            published.append(agent)
            return True

        monkeypatch.setattr(publish_agent_release, "publish", fake_publish)
        monkeypatch.setattr(sys, "argv", ["publish_agent_release.py", "1.33.0"])
        return publish_agent_release.main()

    def test_a_failing_agent_does_not_stop_the_others(self, monkeypatch, capsys):
        published: list[str] = []
        code = self._run(
            monkeypatch,
            capsys,
            published,
            {"beta": publish_agent_release.PublishError("beta: manifest declares no prompt sources")},
        )
        # gamma comes after beta alphabetically: it is the one an abort
        # would have silently skipped.
        assert published == ["alpha", "gamma"]
        # ...and the run still fails, so a half-published release is never
        # reported as a success.
        assert code == 1
        assert "failed: beta" in capsys.readouterr().err

    def test_a_transport_error_is_isolated_too(self, monkeypatch, capsys):
        published: list[str] = []
        code = self._run(
            monkeypatch, capsys, published, {"alpha": publish_agent_release.httpx.ConnectError("boom")}
        )
        assert published == ["beta", "gamma"]
        assert code == 1

    def test_a_v_prefixed_tag_is_refused(self, monkeypatch, capsys):
        """This org's tags carry no v prefix; a v-tag means a caller bug,
        and publishing under it would create rows nothing resolves."""
        monkeypatch.setattr(sys, "argv", ["publish_agent_release.py", "v1.33.0"])
        assert publish_agent_release.main() == 2
