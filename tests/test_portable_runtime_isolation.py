"""Containment checks for the experimental Rust spike (#746 / #759)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = REPO_ROOT / "experiments" / "portable-runtime-rs"

FORBIDDEN = ("experiments/", "mctl-runtime")
GUARDED_DIRS = ("orchestrator", "config", "agents")
GUARDED_FILES = ("Dockerfile", "entrypoint.sh", "pyproject.toml", "uv.lock")
PRUNE_DIRS = {".git", ".venv", "venv", "_gitops", "target", "node_modules", ".claude"}

requires_experiment = pytest.mark.skipif(not EXPERIMENT.is_dir(), reason="experiments/portable-runtime-rs absent")

_COPY_RE = re.compile(r"^\s*(COPY|ADD)\b.*\bexperiments\b", re.IGNORECASE)
_IGNORE_ENTRIES = {"experiments", "experiments/", "experiments/**", "/experiments", "/experiments/"}


def dockerfile_copies_experiments(text: str) -> list[str]:
    joined = re.sub(r"\\[ \t]*\r?\n", " ", text)
    return [line for line in joined.splitlines() if _COPY_RE.match(line)]


def dockerignore_excludes_experiments(text: str) -> bool:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        if line in _IGNORE_ENTRIES:
            return True
    return False


def _guarded_files() -> list[Path]:
    files: list[Path] = []
    for d in GUARDED_DIRS:
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT / d):
            dirnames[:] = [n for n in dirnames if n != "__pycache__"]
            files.extend(Path(dirpath) / f for f in filenames)
    files.extend(REPO_ROOT / f for f in GUARDED_FILES if (REPO_ROOT / f).is_file())
    return files


@requires_experiment
def test_no_reference_in_shipped_paths() -> None:
    hits: list[tuple[str, str]] = []
    for path in _guarded_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits.extend((str(path.relative_to(REPO_ROOT)), n) for n in FORBIDDEN if n in text)
    assert not hits, f"shipped paths reference the experiment: {hits}"


@requires_experiment
def test_dockerfile_does_not_copy_experiments() -> None:
    assert dockerfile_copies_experiments((REPO_ROOT / "Dockerfile").read_text()) == []


@requires_experiment
def test_dockerignore_excludes_experiments() -> None:
    assert dockerignore_excludes_experiments((REPO_ROOT / ".dockerignore").read_text())


@requires_experiment
def test_no_cargo_toml_outside_experiment() -> None:
    stray: list[str] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [n for n in dirnames if n not in PRUNE_DIRS]
        if "Cargo.toml" in filenames:
            p = Path(dirpath)
            if p != EXPERIMENT and EXPERIMENT not in p.parents:
                stray.append(str(p.relative_to(REPO_ROOT)))
    assert not stray, f"Cargo.toml outside the experiment: {stray}"


def test_detector_flags_injected_copy() -> None:
    real = (REPO_ROOT / "Dockerfile").read_text()
    assert dockerfile_copies_experiments(real + "\nCOPY experiments/ ./experiments/\n")
    assert dockerfile_copies_experiments(real + "\nCOPY \\\n  experiments/ ./experiments/\n")
    assert dockerfile_copies_experiments(real) == []
    assert dockerignore_excludes_experiments("experiments/\n")
    assert not dockerignore_excludes_experiments("# experiments/\n")
