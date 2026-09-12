"""Refusal-marker tests for the Tier 2 implementer (mctl-agents#360).

`exit 42` used to mean three different things at once: the implementer
crashed, the findings were already addressed, or the agent deliberately
declined to act on an explicit operator decision recorded on the PR. Only the
first deserves a `MAX_REVIEW_ATTEMPTS` slot. These tests pin the machine-
readable signal that separates them — a JSON marker file the agent writes in
the worktree root, which no accidental behaviour produces — and the exit code
it maps to.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestrator import run_implementer


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo — the marker's untracked check runs `git ls-files`."""
    root = tmp_path / "target"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "init")
    return root


def _write_marker(repo: Path, payload) -> Path:
    path = repo / run_implementer.REFUSAL_MARKER_FILENAME
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# _read_refusal_marker — what counts as a refusal
# ---------------------------------------------------------------------------
def test_valid_marker_yields_the_reason(repo) -> None:
    _write_marker(repo, {
        "refused": True,
        "reason": "Finding 2 is out of scope by explicit operator decision.",
    })
    assert run_implementer._read_refusal_marker(repo) == (
        "Finding 2 is out of scope by explicit operator decision."
    )


def test_no_marker_is_not_a_refusal(repo) -> None:
    assert run_implementer._read_refusal_marker(repo) is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not json at all", id="not-json"),
        pytest.param(["refused"], id="not-an-object"),
        pytest.param({"reason": "no refused key"}, id="missing-refused"),
        pytest.param({"refused": False, "reason": "r"}, id="refused-false"),
        pytest.param({"refused": "true", "reason": "r"}, id="refused-is-a-string"),
        pytest.param({"refused": 1, "reason": "r"}, id="refused-is-truthy-not-true"),
        pytest.param({"refused": True}, id="missing-reason"),
        pytest.param({"refused": True, "reason": "   "}, id="blank-reason"),
        pytest.param({"refused": True, "reason": 42}, id="reason-not-a-string"),
    ],
)
def test_malformed_markers_are_not_refusals(repo, payload) -> None:
    """Anything short of the exact contract falls back to the failure path.

    The asymmetry is deliberate: mistaking a crash for a refusal would let a
    broken PR sit forever without ever reaching `review-stuck`, while
    mistaking a refusal for a failure only costs one attempt.
    """
    _write_marker(repo, payload)
    assert run_implementer._read_refusal_marker(repo) is None


def test_a_committed_marker_is_ignored(repo) -> None:
    """A marker checked into a target repo would exempt it from the cap forever."""
    _write_marker(repo, {"refused": True, "reason": "permanently refusing"})
    _git(repo, "add", "-f", run_implementer.REFUSAL_MARKER_FILENAME)
    _git(repo, "commit", "-qm", "smuggle a marker")
    assert run_implementer._read_refusal_marker(repo) is None


def test_reason_is_normalised_and_bounded(repo) -> None:
    _write_marker(repo, {"refused": True, "reason": "a\n\n  b\t c " + "x" * 5000})
    reason = run_implementer._read_refusal_marker(repo)
    assert reason is not None
    assert reason.startswith("a b c ")
    assert "\n" not in reason
    assert len(reason) == run_implementer.MAX_REFUSAL_REASON_CHARS


# ---------------------------------------------------------------------------
# Exit-code mapping
# ---------------------------------------------------------------------------
def test_refusal_error_maps_to_47() -> None:
    assert run_implementer._review_feedback_exit_code(
        f"{run_implementer.REFUSAL_ERROR_PREFIX} operator said no"
    ) == run_implementer.EXIT_DELIBERATE_NO_OP


def test_existing_sentinels_are_unchanged() -> None:
    """#360 must not reshuffle the codes #12 and #366 rely on."""
    assert run_implementer.EXIT_DELIBERATE_NO_OP == 47
    cases = {
        "": run_implementer.EXIT_OK,
        "implementer produced no follow-up commits":
            run_implementer.EXIT_NO_FOLLOWUP_COMMITS,
        "branch feat/agents-x not found on origin; refusing":
            run_implementer.EXIT_BRANCH_MISSING_ON_ORIGIN,
        "orphaned sub-agent: 1 task still running":
            run_implementer.EXIT_ORPHANED_SUBAGENT,
        "operation timed out: operation exceeded 600s":
            run_implementer.EXIT_OPERATION_TIMEOUT,
        "RuntimeError: boom": run_implementer.EXIT_GENERIC_FAILURE,
    }
    for error, code in cases.items():
        assert run_implementer._review_feedback_exit_code(error) == code, error


# ---------------------------------------------------------------------------
# review_feedback_one — no commits + marker = refusal, no commits alone = failure
# ---------------------------------------------------------------------------
def _stub_review_feedback(monkeypatch, repo: Path) -> None:
    monkeypatch.setattr(run_implementer, "_clone_target", lambda *_a, **_kw: repo)
    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a: True)
    monkeypatch.setattr(run_implementer, "_checkout_existing_branch", lambda *_a: None)
    monkeypatch.setattr(run_implementer, "_stage_implementer_agent", lambda *_a: None)
    monkeypatch.setattr(run_implementer, "_capture_head_sha", lambda *_a: "old")
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: False)
    monkeypatch.setattr(run_implementer.anyio, "run", lambda *_a, **_kw: None)


def _ref(repo: Path) -> run_implementer.ProposalRef:
    return run_implementer.ProposalRef(
        service="mctl-web",
        slug="test-slug",
        proposal_dir=repo,
        status="implemented",
    )


def test_no_commits_with_marker_is_a_deliberate_no_op(repo, monkeypatch) -> None:
    _stub_review_feedback(monkeypatch, repo)
    _write_marker(repo, {"refused": True, "reason": "already addressed in 1a2b3c4"})

    result = run_implementer.review_feedback_one(_ref(repo), {"summaries": []})

    assert result.error is not None
    assert result.error.startswith(run_implementer.REFUSAL_ERROR_PREFIX)
    assert "already addressed in 1a2b3c4" in result.error
    assert run_implementer._review_feedback_exit_code(result.error) == 47


def test_no_commits_without_marker_still_fails_deterministically(repo, monkeypatch) -> None:
    """The silent no-op keeps costing an attempt — that is #12's fix."""
    _stub_review_feedback(monkeypatch, repo)

    result = run_implementer.review_feedback_one(_ref(repo), {"summaries": []})

    assert result.error == "implementer produced no follow-up commits"
    assert run_implementer._review_feedback_exit_code(result.error) == 42


def test_a_commit_beats_the_marker(repo, monkeypatch) -> None:
    """A marker written next to a real commit must not suppress the push."""
    _stub_review_feedback(monkeypatch, repo)
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_push_followup", lambda *_a: None)
    monkeypatch.setattr(run_implementer, "_load_status", lambda *_a: {"pr": "https://pr"})
    _write_marker(repo, {"refused": True, "reason": "should be ignored"})

    result = run_implementer.review_feedback_one(_ref(repo), {"summaries": []})

    assert result.error is None
    assert result.pr_url == "https://pr"


# ---------------------------------------------------------------------------
# main() — the reason reaches the shepherd through --refusal-out
# ---------------------------------------------------------------------------
def test_main_writes_the_refusal_reason_and_exits_47(tmp_path, monkeypatch) -> None:
    ref = _ref(tmp_path)
    bundle = tmp_path / "feedback.json"
    bundle.write_text("{}", encoding="utf-8")
    out = tmp_path / "refusal.json"
    reason = "operator deferred this finding to a separate issue"

    monkeypatch.setattr("sys.argv", [
        "run_implementer.py",
        "--service", "mctl-web",
        "--slug", "test-slug",
        "--state-dir", str(tmp_path),
        "--review-feedback", str(bundle),
        "--refusal-out", str(out),
    ])
    monkeypatch.setattr(run_implementer, "ensure_auth_for_sdk", lambda: None)
    monkeypatch.setattr(run_implementer, "_load_review_feedback", lambda _p: {})
    monkeypatch.setattr(
        run_implementer, "find_accepted_proposals", lambda *_a, **_kw: [ref],
    )
    monkeypatch.setattr(
        run_implementer, "review_feedback_one",
        lambda *_a, **_kw: run_implementer.ImplementResult(
            ref=ref,
            pr_url=None,
            error=f"{run_implementer.REFUSAL_ERROR_PREFIX} {reason}",
        ),
    )

    with pytest.raises(SystemExit) as exc:
        run_implementer.main()

    assert exc.value.code == run_implementer.EXIT_DELIBERATE_NO_OP
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "refused": True, "reason": reason,
    }


def test_main_writes_nothing_for_a_plain_failure(tmp_path, monkeypatch) -> None:
    """--refusal-out stays empty unless the run really was a refusal."""
    ref = _ref(tmp_path)
    bundle = tmp_path / "feedback.json"
    bundle.write_text("{}", encoding="utf-8")
    out = tmp_path / "refusal.json"
    out.write_text("", encoding="utf-8")

    monkeypatch.setattr("sys.argv", [
        "run_implementer.py",
        "--service", "mctl-web",
        "--slug", "test-slug",
        "--state-dir", str(tmp_path),
        "--review-feedback", str(bundle),
        "--refusal-out", str(out),
    ])
    monkeypatch.setattr(run_implementer, "ensure_auth_for_sdk", lambda: None)
    monkeypatch.setattr(run_implementer, "_load_review_feedback", lambda _p: {})
    monkeypatch.setattr(
        run_implementer, "find_accepted_proposals", lambda *_a, **_kw: [ref],
    )
    monkeypatch.setattr(
        run_implementer, "review_feedback_one",
        lambda *_a, **_kw: run_implementer.ImplementResult(
            ref=ref, pr_url=None, error="implementer produced no follow-up commits",
        ),
    )

    with pytest.raises(SystemExit) as exc:
        run_implementer.main()

    assert exc.value.code == run_implementer.EXIT_NO_FOLLOWUP_COMMITS
    assert out.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# The prompt is the other half of the contract
# ---------------------------------------------------------------------------
def test_followup_prompt_specifies_the_marker(tmp_path) -> None:
    """An agent can only honour a contract it was told about."""
    prompt = run_implementer._build_prompt(
        _ref(tmp_path), review_feedback={"p1": True, "summaries": []},
    )
    assert run_implementer.REFUSAL_MARKER_FILENAME in prompt
    assert '"refused": true' in prompt
    assert "operator decision" in prompt


def test_new_branch_prompt_does_not_mention_the_marker(tmp_path) -> None:
    """New-PR runs have no review-attempt budget, so they need no marker."""
    prompt = run_implementer._build_prompt(_ref(tmp_path))
    assert run_implementer.REFUSAL_MARKER_FILENAME not in prompt


# ---------------------------------------------------------------------------
# Review round 1 on #369: keep the marker from ever becoming tracked
#
# The untracked check is the backstop, not the defence. The realistic way a
# marker becomes tracked is a broad `git add -A` from the sub-agent sweeping it
# into a follow-up commit (a commit beats the marker, so the push proceeds and
# it lands on `feat/agents-<slug>`). From the next tick on, every genuine
# refusal on that branch falls back to exit 42 and charges an attempt — #360
# reverted silently and permanently for that PR. `.git/info/exclude` is what
# stops that, the same way it already protects the staged implementer.md.
# ---------------------------------------------------------------------------
def _excluded(repo: Path) -> list[str]:
    return (repo / ".git" / "info" / "exclude").read_text(
        encoding="utf-8"
    ).splitlines()


def test_staging_excludes_the_refusal_marker(repo) -> None:
    run_implementer._stage_implementer_agent(repo, "mctl-web")
    entries = _excluded(repo)
    assert run_implementer.REFUSAL_MARKER_FILENAME in entries
    assert ".claude/agents/implementer.md" in entries


def test_excluded_marker_survives_git_add_dash_a(repo) -> None:
    """The property that matters, asserted against real git, not the file text."""
    run_implementer._stage_implementer_agent(repo, "mctl-web")
    (repo / run_implementer.REFUSAL_MARKER_FILENAME).write_text(
        json.dumps({"refused": True, "reason": "r"}), encoding="utf-8",
    )
    (repo / "src.txt").write_text("a real change\n", encoding="utf-8")
    _git(repo, "add", "-A")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.split()
    assert "src.txt" in staged
    assert run_implementer.REFUSAL_MARKER_FILENAME not in staged
    # ... and the marker is therefore still honoured on the next run.
    assert run_implementer._read_refusal_marker(repo) == "r"


def test_staging_is_idempotent(repo) -> None:
    """Re-staging must not grow the exclude file (clones are per-run, but the
    append is unconditional-looking and cheap to pin)."""
    run_implementer._stage_implementer_agent(repo, "mctl-web")
    first = _excluded(repo)
    run_implementer._stage_implementer_agent(repo, "mctl-web")
    assert _excluded(repo) == first


def test_prompt_example_is_itself_valid_json(tmp_path) -> None:
    """The prompt says "exactly this shape", so the shape shown must parse.

    A multi-line example produces a JSON string with raw newlines, which
    `json.loads` rejects — the agent would refuse correctly, the marker would be
    discarded, and the attempt charged: the precise failure #360 removes.
    """
    prompt = run_implementer._build_prompt(
        _ref(tmp_path), review_feedback={"p1": True, "summaries": []},
    )
    line = next(
        ln.strip() for ln in prompt.splitlines() if ln.strip().startswith('{"refused"')
    )
    payload = json.loads(line)
    assert payload["refused"] is True
    assert isinstance(payload["reason"], str) and payload["reason"]


def test_subagent_definitions_carry_the_marker_contract() -> None:
    """The sub-agent is the one that decides; it has to know the contract.

    The outer prompt alone would rely on the parent noticing the child declined
    and writing the file on its behalf.
    """
    agents_dir = Path(run_implementer.AGENTS_DIR)
    definitions = sorted(agents_dir.glob("*/.claude/agents/implementer.md"))
    assert definitions, "no implementer sub-agent definitions found"
    for path in definitions:
        body = path.read_text(encoding="utf-8")
        assert run_implementer.REFUSAL_MARKER_FILENAME in body, path


# ---------------------------------------------------------------------------
# Review round 2 on #369
# ---------------------------------------------------------------------------
def test_unknown_ls_files_result_is_not_treated_as_untracked(repo, monkeypatch) -> None:
    """"Could not establish the fact" must read as "not a refusal".

    Every other check in `_read_refusal_marker` errs that way; this one used to
    invert it, treating any non-zero `git ls-files` result as untracked. Exit 1
    is the real "not in the index" answer — 128, a missing git, or a corrupt
    index are not answers at all, and honouring a marker exactly when the
    repository state is unknown is the wrong direction for this design.
    """
    _write_marker(repo, {"refused": True, "reason": "r"})
    real_run = run_implementer._run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "ls-files"]:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repository")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(run_implementer, "_run", fake_run)
    assert run_implementer._read_refusal_marker(repo) is None


def test_exit_one_is_still_honoured_as_untracked(repo, monkeypatch) -> None:
    """The narrowing must not break the ordinary case it guards."""
    _write_marker(repo, {"refused": True, "reason": "r"})
    real_run = run_implementer._run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "ls-files"]:
            return subprocess.CompletedProcess(cmd, 1, "", "did not match any file(s)")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(run_implementer, "_run", fake_run)
    assert run_implementer._read_refusal_marker(repo) == "r"


def test_subagent_definitions_exclude_the_blocked_case() -> None:
    """A blocked run is a failed attempt, not a refusal.

    The marker bullet sits directly above "if the proposal is unclear, STOP and
    explain", so without an explicit exclusion an agent could record a genuine
    failure as a deliberate no-op — converting the one case that must charge the
    cap into one that does not, and landing a human on a `review-stuck` note
    asserting "the proposal is not at fault" about a faulty proposal.
    """
    agents_dir = Path(run_implementer.AGENTS_DIR)
    definitions = sorted(agents_dir.glob("*/.claude/agents/implementer.md"))
    assert definitions, "no implementer sub-agent definitions found"
    for path in definitions:
        body = path.read_text(encoding="utf-8")
        assert "BLOCKED" in body, path
        assert "do NOT write the marker" in body, path


# ---------------------------------------------------------------------------
# Review round 3 on #369 (agy P2): the marker is untrusted in SIZE too
#
# The file sits in a cloned target repo's worktree and is written by an LLM
# holding a Bash tool. A redirect into the wrong path, a loop that appends, or a
# stray `tee` makes it enormous without any malice, and `read_text()` would pull
# all of it into the orchestrator. An implementer OOM is the least diagnosable
# failure available here: memory pressure on that workload is a live issue and
# an OOMKill does not surface in `last_terminated_reason`.
# ---------------------------------------------------------------------------
def test_the_read_itself_is_bounded(repo, monkeypatch) -> None:
    """The cap must bind the READ, not a syscall that preceded it.

    `stat()` then `read_text()` establishes a size and then reads to EOF: the
    marker lives in the agent's own workspace and the agent holds a Bash tool,
    so a background appender (a stray loop, no malice needed) passes the check
    and then makes the read grow without bound. A test cannot fix that shape —
    it can only document it — so this asserts the property that replaces it:
    the function never asks the file for more than one byte past the cap.
    """
    _write_marker(repo, {"refused": True, "reason": "r"})
    cap = run_implementer.MAX_REFUSAL_MARKER_BYTES
    requested = []
    real_open = Path.open

    class _CountingHandle:
        def __init__(self, fh):
            self._fh = fh

        def read(self, size=-1):
            requested.append(size)
            return self._fh.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self._fh.close()
            return False

    def counting_open(self, *a, **kw):
        return _CountingHandle(real_open(self, *a, **kw))

    monkeypatch.setattr(Path, "open", counting_open)
    assert run_implementer._read_refusal_marker(repo) == "r"
    assert requested == [cap + 1], (
        "the marker must be read with an explicit bound, never to EOF"
    )


def test_an_enormous_marker_is_refused(repo) -> None:
    """A real file far past the cap is rejected, and quickly."""
    path = repo / run_implementer.REFUSAL_MARKER_FILENAME
    path.write_bytes(b"x" * (run_implementer.MAX_REFUSAL_MARKER_BYTES * 80))
    assert run_implementer._read_refusal_marker(repo) is None


def test_an_unreadable_marker_is_not_a_crash(repo, monkeypatch) -> None:
    """`is_file()` then `open()` is still two syscalls; the file can vanish."""
    _write_marker(repo, {"refused": True, "reason": "r"})

    def gone(*_a, **_kw):
        raise FileNotFoundError("vanished")

    monkeypatch.setattr(Path, "open", gone)
    assert run_implementer._read_refusal_marker(repo) is None

def test_oversized_marker_falls_back_to_the_charged_path(repo, monkeypatch) -> None:
    """Refusing the marker must leave the ordinary failure intact, not a crash."""
    _stub_review_feedback(monkeypatch, repo)
    (repo / run_implementer.REFUSAL_MARKER_FILENAME).write_text(
        "y" * (run_implementer.MAX_REFUSAL_MARKER_BYTES + 1), encoding="utf-8",
    )

    result = run_implementer.review_feedback_one(_ref(repo), {"summaries": []})

    assert result.error == "implementer produced no follow-up commits"
    assert run_implementer._review_feedback_exit_code(result.error) == 42


def test_a_marker_at_the_cap_is_still_honoured(repo) -> None:
    """The bound must not clip a legitimate marker — the boundary is inclusive."""
    payload = {"refused": True, "reason": "operator decision"}
    envelope = json.dumps(payload)
    pad = run_implementer.MAX_REFUSAL_MARKER_BYTES - len(envelope.encode("utf-8"))
    assert pad > 0
    # Pad with whitespace inside the JSON so the file is exactly at the cap and
    # still parses.
    padded = envelope[:-1] + (" " * pad) + "}"
    path = repo / run_implementer.REFUSAL_MARKER_FILENAME
    path.write_text(padded, encoding="utf-8")
    assert path.stat().st_size == run_implementer.MAX_REFUSAL_MARKER_BYTES

    assert run_implementer._read_refusal_marker(repo) == "operator decision"


def _deeply_nested_marker(repo: Path, depth: int = 20_000) -> Path:
    """A marker that is small, syntactically valid, and blows the C stack."""
    path = repo / run_implementer.REFUSAL_MARKER_FILENAME
    payload = '{"refused": true, "reason": ' + "[" * depth + "]" * depth + "}"
    path.write_text(payload, encoding="utf-8")
    assert path.stat().st_size < run_implementer.MAX_REFUSAL_MARKER_BYTES, (
        "the point of this test is a payload the SIZE cap lets through"
    )
    return path


def test_deeply_nested_marker_is_ignored_not_raised(repo) -> None:
    _deeply_nested_marker(repo)
    assert run_implementer._read_refusal_marker(repo) is None


def test_nested_marker_falls_back_to_the_charged_path(repo, monkeypatch) -> None:
    """The destination is what matters: 42, not the counter-less exit 1."""
    _stub_review_feedback(monkeypatch, repo)
    _deeply_nested_marker(repo)

    result = run_implementer.review_feedback_one(_ref(repo), {"summaries": []})

    assert result.error == "implementer produced no follow-up commits"
    assert run_implementer._review_feedback_exit_code(result.error) == 42


@pytest.mark.parametrize(
    "exc",
    [
        RecursionError("maximum recursion depth exceeded"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        MemoryError(),
        RuntimeError("something nobody enumerated"),
    ],
    ids=["recursion", "unicode", "memory", "unknown"],
)
def test_any_read_failure_means_not_a_refusal(repo, monkeypatch, exc) -> None:
    """The property, stated once: no exception type escapes this function.

    Pinning the class rather than today's four members is the whole point — the
    next exception nobody enumerated must land on the charged path too, without
    anyone having to add it to a tuple first.
    """
    _write_marker(repo, {"refused": True, "reason": "r"})

    def boom(*_a, **_kw):
        raise exc

    monkeypatch.setattr(Path, "open", boom)
    assert run_implementer._read_refusal_marker(repo) is None


def test_a_failing_refusal_write_still_exits_47(tmp_path, monkeypatch) -> None:
    """`_write_refusal_out` runs just before `sys.exit(47)`.

    An escape there would swap a correctly-classified refusal for an uncaught
    traceback and exit 1 — the counter-less arm — over prose that is advisory
    by design.
    """
    ref = _ref(tmp_path)
    bundle = tmp_path / "feedback.json"
    bundle.write_text("{}", encoding="utf-8")

    monkeypatch.setattr("sys.argv", [
        "run_implementer.py",
        "--service", "mctl-web",
        "--slug", "test-slug",
        "--state-dir", str(tmp_path),
        "--review-feedback", str(bundle),
        "--refusal-out", str(tmp_path / "refusal.json"),
    ])
    monkeypatch.setattr(run_implementer, "ensure_auth_for_sdk", lambda: None)
    monkeypatch.setattr(run_implementer, "_load_review_feedback", lambda _p: {})
    monkeypatch.setattr(
        run_implementer, "find_accepted_proposals", lambda *_a, **_kw: [ref],
    )
    monkeypatch.setattr(
        run_implementer, "review_feedback_one",
        lambda *_a, **_kw: run_implementer.ImplementResult(
            ref=ref,
            pr_url=None,
            error=f"{run_implementer.REFUSAL_ERROR_PREFIX} operator decision",
        ),
    )

    def boom(*_a, **_kw):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(Path, "write_text", boom)

    with pytest.raises(SystemExit) as exc:
        run_implementer.main()

    assert exc.value.code == run_implementer.EXIT_DELIBERATE_NO_OP
