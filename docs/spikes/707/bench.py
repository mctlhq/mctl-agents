#!/usr/bin/env python3
"""Benchmark harness for the #707 Pyrefly-vs-mypy spike (stdlib only).

Throwaway measurement tooling; it gates nothing. Run it against a clean
`git worktree` of the pinned baseline SHA, with the assets (this directory)
coming from the spike branch:

    git worktree add --detach /tmp/w707 68f10afcb8a96d7b1c54d5d56819d8e39979c275
    (cd /tmp/w707 && uv sync --locked)
    uv run --locked python docs/spikes/707/bench.py \
        --target /tmp/w707 --out docs/spikes/707

Modes (default: all): diagnostics, cold, edit, loop, lint-job.
The harness refuses to start on a dirty target tree and reverts every
mutation patch in a `finally` block. Pyrefly is only ever invoked as
`uv tool run pyrefly==1.4.0`, so it never enters pyproject.toml / uv.lock.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

PINNED_SHA = "68f10afcb8a96d7b1c54d5d56819d8e39979c275"
PYREFLY = ["uv", "tool", "run", "pyrefly==1.4.0"]
MYPY = ["uv", "run", "--locked", "--no-sync", "mypy"]
ASSETS = Path(__file__).resolve().parent
COLD_RUNS = 10
EDIT_ITERATIONS = 20
# Mutations used for the loop sequence, with the targeted test for each file.
LOOP_PATCHES = [
    "01-optional-to-nonoptional",
    "02-incompatible-assignment",
    "03-wrong-argument-type",
    "05-attribute-on-optional",
    "06-unawaited-coroutine",
]
LOOP_TESTS = {
    "orchestrator/ci_checks.py": "tests/test_ci_checks.py",
    "orchestrator/temporal/worker.py": "tests/test_worker_schedules.py",
}


def sh(cmd, cwd, env=None, check=False):
    """Run cmd, return (rc, seconds, combined output)."""
    t0 = time.perf_counter()
    p = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)
    dt = time.perf_counter() - t0
    if check and p.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {p.stdout}{p.stderr}")
    return p.returncode, dt, p.stdout + p.stderr


def stats(samples):
    s = sorted(samples)
    p95 = s[max(0, math.ceil(0.95 * len(s)) - 1)]
    return {
        "n": len(s),
        "median_s": round(statistics.median(s), 4),
        "p95_s": round(p95, 4),
        "min_s": round(s[0], 4),
        "max_s": round(s[-1], 4),
    }


def config_b(target: Path) -> Path:
    """Config (B) placed inside the target tree.

    pyrefly.toml paths are relative to the file itself, so the config must
    live at the same depth inside the pinned worktree; read from ASSETS it
    would resolve against the spike checkout (no .venv, wrong SHA).
    """
    dest = target / "docs/spikes/707/pyrefly.toml"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ASSETS / "pyrefly.toml", dest)
    return dest


def checker_cmds(target: Path):
    return {
        "mypy": MYPY,
        "pyrefly-A": PYREFLY + ["check", "--output-format", "min-text"],
        "pyrefly-B": PYREFLY + ["check", "--config", str(config_b(target)), "--output-format", "min-text"],
    }


def clear_mypy_cache(target: Path):
    shutil.rmtree(target / ".mypy_cache", ignore_errors=True)


def require_clean(target: Path):
    rc, _, out = sh(["git", "status", "--porcelain", "--untracked-files=no"], target)
    if out.strip():
        raise SystemExit(f"refusing to run: target tree is dirty:\n{out}")
    rc, _, head = sh(["git", "rev-parse", "HEAD"], target, check=True)
    if head.strip() != PINNED_SHA:
        raise SystemExit(f"target HEAD {head.strip()} is not the pinned {PINNED_SHA}")


class Patched:
    """Context manager: apply a patch, always revert it."""

    def __init__(self, target: Path, patch: Path):
        self.target, self.patch = target, patch

    def __enter__(self):
        sh(["git", "apply", str(self.patch)], self.target, check=True)
        return self

    def __exit__(self, *exc):
        sh(["git", "apply", "-R", str(self.patch)], self.target, check=True)


def patch_file(patch: Path) -> str:
    m = re.search(r"^\+\+\+ b/(\S+)", patch.read_text(), re.M)
    assert m, patch
    return m.group(1)


def environment(target: Path):
    def first(cmd):
        try:
            return sh(cmd, target)[2].strip().splitlines()[0]
        except Exception as exc:  # noqa: BLE001
            return f"unavailable: {exc}"

    cpu = "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    mem_kb = 0
    try:
        mem_kb = int(re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text()).group(1))
    except (OSError, AttributeError):
        pass
    return {
        "pinned_sha": PINNED_SHA,
        "python": first([str(target / ".venv/bin/python"), "--version"]),
        "uv": first(["uv", "--version"]),
        "mypy": first(MYPY + ["--version"]),
        "pyrefly": first(PYREFLY + ["--version"]),
        "ruff": first(["uv", "run", "--locked", "--no-sync", "ruff", "--version"]),
        "cpu_model": cpu,
        "cores": os.cpu_count(),
        "memory_mb": mem_kb // 1024,
        "os": platform.platform(),
    }


def mypy_file_list(target: Path):
    """Files mypy checked, from `mypy -v` 'Found source' lines."""
    clear_mypy_cache(target)
    _, _, out = sh(MYPY + ["-v"], target)
    files = set()
    for m in re.finditer(r"Found source:.*?path='([^']+)'", out):
        files.add(m.group(1).lstrip("./"))
    return sorted(files)


def pyrefly_file_list(target: Path, config: Path | None):
    cmd = PYREFLY + ["dump-config", "--max-files", "all"]
    if config:
        cmd += ["--config", str(config)]
    _, _, out = sh(cmd, target)
    files = []
    root = str(target) + "/"
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("/") and s.endswith(".py"):
            files.append(s.replace(root, ""))
    return sorted(files), out


def run_diagnostics(target: Path, out: Path):
    d = out / "diagnostics"
    d.mkdir(exist_ok=True)
    res = {}
    cmds = checker_cmds(target)
    for name, cmd in cmds.items():
        if name == "mypy":
            clear_mypy_cache(target)
        rc, dt, text = sh(cmd, target)
        # The working tree path is noise; keep output reproducible.
        if "Invalid site-packages-path" in text or "Cannot find module `temporalio" in text:
            raise SystemExit(f"{name} cannot see the target venv; refusing to record it")
        text = text.replace(str(target) + "/", "")
        (d / f"{name}.txt").write_text(text)
        res[name] = {
            "exit_code": rc,
            "diagnostics": len(re.findall(r"^(?:ERROR|[^\s:]+:\d+: error)", text, re.M)),
            "seconds": round(dt, 3),
        }
    m_files = mypy_file_list(target)
    a_files, a_dump = pyrefly_file_list(target, None)
    b_files, b_dump = pyrefly_file_list(target, config_b(target))
    # A config that reads outside the target, or cannot see its venv, is a
    # failed measurement, not a smaller or noisier result.
    outside = [f for f in a_files + b_files if f.startswith("/")]
    if outside or "Invalid site-packages-path" in b_dump:
        raise SystemExit(f"config B does not resolve inside {target}: {outside[:3]}")
    (d / "pyrefly-A-config-dump.txt").write_text(a_dump.replace(str(target) + "/", ""))
    (d / "pyrefly-B-config-dump.txt").write_text(b_dump.replace(str(target) + "/", ""))
    (d / "files-mypy.txt").write_text("\n".join(m_files) + "\n")
    (d / "files-pyrefly-A.txt").write_text("\n".join(a_files) + "\n")
    (d / "files-pyrefly-B.txt").write_text("\n".join(b_files) + "\n")
    res["file_sets"] = {
        "mypy": len(m_files),
        "pyrefly-A": len(a_files),
        "pyrefly-B": len(b_files),
        "mypy_minus_B": sorted(set(m_files) - set(b_files)),
        "B_minus_mypy": sorted(set(b_files) - set(m_files)),
        "mypy_minus_A": sorted(set(m_files) - set(a_files)),
        "A_minus_mypy": sorted(set(a_files) - set(m_files)),
    }
    return res


def run_cold(target: Path):
    cmds = checker_cmds(target)
    res = {}
    for name, cmd in cmds.items():
        samples = []
        for _ in range(COLD_RUNS):
            if name == "mypy":
                clear_mypy_cache(target)
            samples.append(sh(cmd, target)[1])
        res[name] = stats(samples)
    # Warm mypy: incremental cache kept (labelled; not comparable to cold).
    clear_mypy_cache(target)
    sh(MYPY, target)
    res["mypy-warm-cache"] = stats([sh(MYPY, target)[1] for _ in range(COLD_RUNS)])
    return res


def run_edit(target: Path):
    patches = sorted((ASSETS / "mutations").glob("*.patch"))[:7]
    cmds = checker_cmds(target)
    res = {}
    for name, cmd in cmds.items():
        samples, caught = [], 0
        if name == "mypy":
            clear_mypy_cache(target)
            sh(MYPY, target)  # prime the incremental cache once
        for i in range(EDIT_ITERATIONS):
            patch = patches[i % len(patches)]
            with Patched(target, patch):
                _, dt, text = sh(cmd, target)
            samples.append(dt)
            # Exit status is useless here: Pyrefly already exits 1 on the clean
            # baseline. Count iterations with an error on the mutated file.
            caught += any(patch_file(patch) in ln and "error" in ln.lower() for ln in text.splitlines())
        res[name] = {**stats(samples), "iterations_caught": caught}
    return res


def run_mutation_table(target: Path):
    cmds = checker_cmds(target)
    rows = {}
    sh(MYPY, target)  # prime the incremental cache; detection, not latency
    for patch in sorted((ASSETS / "mutations").glob("*.patch")):
        row = {"file": patch_file(patch)}
        with Patched(target, patch):
            for name, cmd in cmds.items():
                rc, _, text = sh(cmd, target)
                text = text.replace(str(target) + "/", "")
                hits = [ln for ln in text.splitlines() if patch_file(patch) in ln and ("error" in ln.lower())]
                row[name] = {"caught": bool(hits), "diagnostics": hits[:3]}
        rows[patch.stem] = row
    return rows


def run_loop(target: Path):
    cmds = {k: v for k, v in checker_cmds(target).items() if k != "pyrefly-A"}
    res = {}
    for stem in LOOP_PATCHES:
        patch = ASSETS / "mutations" / f"{stem}.patch"
        f = patch_file(patch)
        test = LOOP_TESTS.get(f)
        res[stem] = {}
        for name, cmd in cmds.items():
            if name == "mypy":
                # Warm on the clean tree, as in an agent session, so the timed
                # run below re-checks the edited module instead of hitting a
                # cache built from the already-mutated tree.
                sh(MYPY, target)
            with Patched(target, patch):
                _, t_check, _ = sh(cmd, target)
                _, t_ruff, _ = sh(["uv", "run", "--locked", "--no-sync", "ruff", "check", f], target)
                entry = {
                    "checker_s": round(t_check, 3),
                    "ruff_s": round(t_ruff, 3),
                    "static_feedback_s": round(t_check + t_ruff, 3),
                }
                if test and (target / test).exists():
                    _, t_test, _ = sh(["uv", "run", "--locked", "--no-sync", "pytest", "-q", "-x", test], target)
                    entry["pytest_s"] = round(t_test, 3)
                    entry["pytest_file"] = test
                res[stem][name] = entry
    return res


def du_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            total += p.stat().st_size
    return round(total / 1e6, 1)


def run_lint_job(target: Path):
    """One lint-job-equivalent timing from a clean clone, phases separate."""
    res = {}
    with tempfile.TemporaryDirectory() as td:
        clone = Path(td) / "clone"
        sh(["git", "clone", "--no-hardlinks", "-q", str(target), str(clone)], td, check=True)
        env = {
            **os.environ,
            "UV_CACHE_DIR": str(Path(td) / "uv-cache"),
            "UV_TOOL_DIR": str(Path(td) / "uv-tools"),
            "UV_PROJECT_ENVIRONMENT": str(clone / ".venv"),
        }
        res["uv_sync_locked_s"] = round(sh(["uv", "sync", "--locked"], clone, env, True)[1], 3)
        res["ruff_s"] = round(
            sh(["uv", "run", "--locked", "ruff", "check", "orchestrator", "config", "tests", "tools"], clone, env)[1],
            3,
        )
        rc, dt, _ = sh(["uv", "run", "--locked", "mypy"], clone, env)
        res["mypy_s"], res["mypy_exit"] = round(dt, 3), rc
        res["lint_job_total_s"] = round(res["uv_sync_locked_s"] + res["ruff_s"] + res["mypy_s"], 3)
        # Pyrefly delivery cost on an empty uv cache (the `uvx` path).
        cache = Path(td) / "uv-cache"
        before = du_mb(cache) if cache.exists() else 0.0
        t = sh(PYREFLY + ["--version"], clone, env)[1]
        res["pyrefly_cold_fetch_s"] = round(t, 3)
        res["pyrefly_download_cache_delta_mb"] = round(du_mb(cache) - before, 1)
        res["pyrefly_tool_env_mb"] = du_mb(Path(td) / "uv-tools") if (Path(td) / "uv-tools").exists() else None
        site = clone / ".venv/lib/python3.12/site-packages"
        res["mypy_install_mb"] = round(sum(du_mb(site / p) for p in ("mypy", "mypyc") if (site / p).exists()), 1)
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--target", required=True, type=Path, help="clean worktree at the pinned SHA, already `uv sync --locked`"
    )
    ap.add_argument("--out", type=Path, default=ASSETS, help="where results.json and diagnostics/ are written")
    ap.add_argument("--modes", nargs="*", default=["diagnostics", "cold", "edit", "mutations", "loop", "lint-job"])
    a = ap.parse_args()
    target = a.target.resolve()
    require_clean(target)
    a.out.mkdir(parents=True, exist_ok=True)
    results = {"environment": environment(target)}
    try:
        if "diagnostics" in a.modes:
            results["diagnostics"] = run_diagnostics(target, a.out)
        if "cold" in a.modes:
            results["cold"] = run_cold(target)
        if "edit" in a.modes:
            results["edit"] = run_edit(target)
        if "mutations" in a.modes:
            results["mutations"] = run_mutation_table(target)
        if "loop" in a.modes:
            results["loop"] = run_loop(target)
        if "lint-job" in a.modes:
            results["lint_job"] = run_lint_job(target)
    finally:
        clear_mypy_cache(target)
        shutil.rmtree(target / "docs/spikes/707", ignore_errors=True)
        (a.out / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    require_clean(target)
    print(json.dumps(results["environment"], indent=2))


if __name__ == "__main__":
    main()
