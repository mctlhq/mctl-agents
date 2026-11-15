"""`$`-anchored validators must use `fullmatch`, not `match`/`search`.

In Python's `re`, `$` also matches just before a trailing newline, so
`re.compile(r"^[a-z]+$").match("abc\\n")` succeeds. A validator written that
way accepts `"mctlhq/repo\\n"` as an `owner/name` and passes the newline on to
whatever builds a path, a URL or a log line from it. `fullmatch` does not.

Two checks:

- a static guard over the source tree, so a new `.match` on an anchored
  pattern fails here instead of reaching review;
- behaviour checks on the validators that guard values crossing a trust
  boundary (ledger correlation, collected usage records, trace ids,
  commerce times), each fed a valid value with a trailing newline.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNED_DIRS = ("orchestrator", "tools", "config")

#: Files whose exact bytes are a released identity: a context strategy's
#: `implementationHash` (`context_release.IMPLEMENTATION_FILES_BY_STRATEGY`)
#: is sha256 over them, and the committed bindings pin it, so ANY edit
#: unbinds the shipped strategy until a new version is published. Their
#: anchored `.match` calls are fixed together with the next strategy
#: publish, not in a sweep. The pinned counts make a NEW such call fail
#: here all the same; lower a count when a publish fixes the call.
RELEASE_HASHED_EXEMPT = {
    "orchestrator/context_assembly.py": 2,  # _SAFE_SOURCE_ID: source-id codes
    "orchestrator/context_snapshot.py": 1,  # _CONFLICT_SUBJECT_PATTERN
}


def _anchored_pattern(call: ast.Call) -> str | None:
    """The pattern of `re.compile("...$")`, unless MULTILINE makes `$` a line end."""
    if not call.args or not isinstance(call.args[0], ast.Constant) or not isinstance(call.args[0].value, str):
        return None
    pattern = call.args[0].value
    if not pattern.endswith("$") or pattern.endswith("\\$") or "(?m" in pattern:
        return None
    for flag in [*call.args[1:], *(kw.value for kw in call.keywords)]:
        text = ast.unparse(flag)
        if "MULTILINE" in text or "re.M" in text:
            return None
    return pattern


def _module_file(root: Path, module: str) -> Path | None:
    candidate = root.joinpath(*module.split(".")).with_suffix(".py")
    return candidate if candidate.is_file() else None


def _local_anchored(tree: ast.AST) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Call):
            continue
        if ast.unparse(node.value.func) != "re.compile":
            continue
        pattern = _anchored_pattern(node.value)
        if pattern is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            names[ast.unparse(target).split(".")[-1]] = pattern
    return names


def find_anchored_match_calls(root: Path) -> list[str]:
    """`file:line` of every `.match`/`.search` on a `$`-anchored pattern.

    A name is resolved in its own module, or through `from module import
    name` to the module that compiles it, so two unrelated patterns that
    share a name in different files are not confused.
    """
    files = [p for d in SCANNED_DIRS if (root / d).is_dir() for p in sorted((root / d).rglob("*.py"))]
    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in files}
    local = {path: _local_anchored(tree) for path, tree in trees.items()}

    hits: list[str] = []
    for path, tree in trees.items():
        visible = dict(local[path])
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                source = _module_file(root, node.module)
                if source is None or source not in local:
                    continue
                for alias in node.names:
                    if alias.name in local[source]:
                        visible[alias.asname or alias.name] = local[source][alias.name]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("match", "search"):
                continue
            owner = ast.unparse(node.func.value)
            if owner == "re":
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    fake = ast.Call(func=node.func, args=list(node.args), keywords=list(node.keywords))
                    if _anchored_pattern(fake) is not None:
                        hits.append(f"{path.relative_to(root)}:{node.lineno}")
                continue
            if owner.split(".")[-1] in visible:
                hits.append(f"{path.relative_to(root)}:{node.lineno}")
    return hits


def test_release_hashed_exemptions_are_the_hashed_files():
    from orchestrator.context_release import IMPLEMENTATION_FILES_BY_STRATEGY

    hashed = {path for files in IMPLEMENTATION_FILES_BY_STRATEGY.values() for path in files}
    assert set(RELEASE_HASHED_EXEMPT) <= hashed


def test_no_match_or_search_on_a_dollar_anchored_pattern():
    all_hits = find_anchored_match_calls(REPO_ROOT)
    exempt_counts = {
        path: sum(1 for hit in all_hits if hit.rsplit(":", 1)[0] == path) for path in RELEASE_HASHED_EXEMPT
    }
    assert exempt_counts == RELEASE_HASHED_EXEMPT
    hits = [hit for hit in all_hits if hit.rsplit(":", 1)[0] not in RELEASE_HASHED_EXEMPT]
    assert hits == [], (
        "use .fullmatch() on a `$`-anchored pattern; `$` also matches before a "
        "trailing newline, so .match()/.search() accept 'value\\n':\n" + "\n".join(hits)
    )


def test_guard_flags_match_on_an_anchored_pattern(tmp_path: Path):
    """The guard is red on the defect it exists for, not only green on a clean tree."""
    pkg = tmp_path / "orchestrator"
    pkg.mkdir()
    (pkg / "patterns.py").write_text('import re\nREPO_RE = re.compile(r"^[a-z]+/[a-z]+$")\n')
    (pkg / "user.py").write_text(
        "import re\n"
        "from orchestrator.patterns import REPO_RE\n"
        '_LOCAL = re.compile(r"^x$")\n'
        '_LINES = re.compile(r"^x$", re.MULTILINE)\n'
        '_OPEN = re.compile(r"^x")\n'
        "def a(v): return REPO_RE.match(v)\n"
        "def b(v): return _LOCAL.search(v)\n"
        "def c(v): return _LINES.match(v)\n"
        "def d(v): return _OPEN.match(v)\n"
        'def e(v): return re.match(r"^y$", v)\n'
        "def f(v): return REPO_RE.fullmatch(v)\n"
    )
    assert find_anchored_match_calls(tmp_path) == [
        "orchestrator/user.py:6",
        "orchestrator/user.py:7",
        "orchestrator/user.py:10",
    ]


def test_ledger_correlation_drops_values_with_a_trailing_newline():
    from orchestrator.usage_ledger import _checked_correlation

    clean = _checked_correlation({"target_repo": "mctlhq/mctl-agents", "execution_id": "ex-1"})
    assert clean == {"target_repo": "mctlhq/mctl-agents", "execution_id": "ex-1"}
    dirty = _checked_correlation({"target_repo": "mctlhq/mctl-agents\n", "execution_id": "ex-1\n"})
    assert "target_repo" not in dirty
    assert "execution_id" not in dirty


def test_collected_usage_record_drops_target_repo_with_a_trailing_newline():
    import json

    from orchestrator.run_usage_collector import sanitise

    fixture = Path(__file__).parent / "fixtures" / "model-usage-records.json"
    raw = json.loads(fixture.read_text())["records"][0]
    sanitised = sanitise({**raw, "target_repo": raw["target_repo"] + "\n"})
    assert sanitised is not None
    assert "target_repo" not in sanitised


def _trace_id(value: str) -> str:
    from orchestrator.execution_identity import _require_trace_id

    return _require_trace_id(value, where="trace_id")


def _hashed_time(value: str) -> str:
    from orchestrator.commerce import hashed_time

    return hashed_time(value)


@pytest.mark.parametrize(
    ("check", "valid"),
    [
        (_trace_id, "0123456789abcdef0123456789abcdef"),
        (_hashed_time, "2026-01-01T00:00:00Z"),
    ],
    ids=["trace-id", "hashed-time"],
)
def test_strict_validators_reject_a_trailing_newline(check, valid: str):
    assert check(valid) == valid
    with pytest.raises(Exception, match=r"must be|is not"):
        check(valid + "\n")
