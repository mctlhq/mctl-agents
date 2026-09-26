"""Read-only guarantee: the collector never mutates GitHub.

A source-level check, in the spirit of the existing `ast`-based checks in
tests/test_usage_ledger.py: every `gh` invocation this module builds is
`gh api` against a read endpoint, with no `-X`/`--method` write verb
anywhere in the module, and the module never calls `gh issue`, `gh pr`,
`gh run`, `git push` or `git commit`.
"""
from __future__ import annotations

import ast
from pathlib import Path

from orchestrator import run_usage_collector

SOURCE = Path(run_usage_collector.__file__).read_text()
TREE = ast.parse(SOURCE)


def test_no_write_verb_flag_as_a_string_literal_anywhere_in_source():
    """`-X`/`--method` would appear as their own string-literal argv tokens
    if this module ever built a mutating `gh api` call — checked as whole
    AST string constants, not a substring scan, so this cannot be tripped by
    the module's own docstring describing the invariant."""
    literals = {node.value for node in ast.walk(TREE) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert "-X" not in literals
    assert "--method" not in literals


def test_no_mutating_gh_or_git_subcommand_call_in_source():
    """No `gh issue`/`gh pr`/`gh run` call, and no `git push`/`git commit`
    call — checked as actual `ast.Call` argv, not a substring scan of the
    module's own docstring (which names these as the invariant it holds)."""
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call) or not node.args or not isinstance(node.args[0], ast.List):
            continue
        argv = node.args[0]
        tokens = [e.value for e in argv.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if len(tokens) < 2:
            continue
        if tokens[0] == "gh":
            assert tokens[1] not in ("issue", "pr", "run"), tokens
        if tokens[0] == "git":
            assert tokens[1] not in ("push", "commit"), tokens


def test_every_run_gh_helper_call_is_gh_api():
    """`_run_gh(...)` prepends "gh" itself (see its definition below); every
    call site's own argv must start with "api", not `issue`/`pr`/`run`."""
    calls = [
        node for node in ast.walk(TREE)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_run_gh"
    ]
    assert len(calls) >= 2, "expected at least the artifact-listing and auth-check calls"
    for call in calls:
        argv = call.args[0]
        assert isinstance(argv, ast.List) and argv.elts, f"_run_gh call with no literal argv: {ast.dump(call)}"
        first = argv.elts[0]
        assert isinstance(first, ast.Constant) and first.value == "api", (
            f"_run_gh call not starting with 'api': {ast.dump(argv)}"
        )


def test_run_gh_itself_prepends_gh_and_nothing_else():
    """The one place "gh" is prepended: `run_capturing(["gh", *args])`. No
    other literal is spliced in ahead of the caller's own argv."""
    calls = [
        node for node in ast.walk(TREE)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run_capturing"
    ]
    assert len(calls) == 1
    argv = calls[0].args[0]
    assert isinstance(argv, ast.List)
    literal_prefix = [e.value for e in argv.elts if isinstance(e, ast.Constant)]
    assert literal_prefix == ["gh"]


def test_the_one_raw_subprocess_call_downloads_the_artifact_zip_over_get():
    """The single call that bypasses `_run_gh` (binary-safe zip download)
    must itself be `gh api .../zip` — still a read, never a write."""
    calls = [
        node for node in ast.walk(TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and getattr(node.func.value, "id", None) == "subprocess"
    ]
    assert len(calls) == 1
    argv = calls[0].args[0]
    assert isinstance(argv, ast.List)
    literals = [e.value for e in argv.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    assert literals[:2] == ["gh", "api"]
    joined_parts = [
        e.value for e in ast.walk(argv)
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    ]
    assert any("zip" in part for part in joined_parts)
