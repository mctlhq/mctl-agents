"""Pins the inertness of the authoring-canary fixture (mctlhq/mctl-agents#596)."""
import ast
import os
from pathlib import Path

import pytest

from orchestrator import manifest as manifest_mod
from orchestrator import validate_manifest
from orchestrator.options import AUTHORING_CANARY_BUDGET_USD, build_authoring_canary_options

REPO = Path(__file__).resolve().parent.parent
MODULE = REPO / "orchestrator" / "run_authoring_canary.py"
MANIFEST = REPO / "agents" / "_manifests" / "authoring-canary" / "agent.yaml"
READ_ONLY = {"Read", "Glob", "Grep"}
NAMES = ("run_authoring_canary", "authoring_canary", "authoring-canary")


def test_builder_is_read_only(monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "dummy")
    options = build_authoring_canary_options(Path("/nonexistent"), "dummy")
    assert set(options.allowed_tools) == READ_ONLY
    assert not any(t.startswith("mcp__") for t in options.allowed_tools)
    assert not options.mcp_servers
    assert not options.hooks
    assert options.permission_mode not in ("acceptEdits", "bypassPermissions")
    assert options.max_budget_usd == AUTHORING_CANARY_BUDGET_USD


def test_manifest_tool_policy_is_read_only_and_budget_is_minimal():
    m = manifest_mod.load(MANIFEST)
    assert set(m.tool_allow) == READ_ONLY
    assert m.budget_usd == AUTHORING_CANARY_BUDGET_USD == 0.01
    assert m.timeout_seconds is None
    assert m.model_policy_legacy_env_override is None
    assert m.api_version == "agents.mctl.ai/v1alpha1"


def test_entrypoint_has_no_python_caller():
    for top in ("orchestrator", "tools", "config"):
        for path in (REPO / top).rglob("*.py"):
            if path == MODULE:
                continue
            text = path.read_text(encoding="utf-8")
            assert "run_authoring_canary" not in text, path
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.ImportFrom):
                    assert "run_authoring_canary" not in (node.module or ""), path
                elif isinstance(node, ast.Import):
                    assert all("run_authoring_canary" not in a.name for a in node.names), path


def test_entrypoint_is_not_wired_in_packaging_or_ci():
    files = [REPO / "entrypoint.sh", REPO / "Dockerfile", REPO / "pyproject.toml"]
    files += list((REPO / ".github" / "workflows").glob("*"))
    for path in files:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not any(n in text for n in NAMES), path


def test_entrypoint_is_not_wired_in_gitops():
    cwft_dir = validate_manifest.GITOPS_CWFT_DIR
    if not cwft_dir.is_dir():
        if os.environ.get("CI"):
            pytest.fail(f"{cwft_dir} not found; pr-validation.yml checks mctl-gitops out")
        pytest.skip(f"{cwft_dir} not found")
    for path in [*cwft_dir.glob("cwft-*.yaml"), *cwft_dir.parent.glob("cronworkflow-*.yaml"),
                 *cwft_dir.glob("cronworkflow-*.yaml")]:
        text = path.read_text(encoding="utf-8")
        assert not any(n in text for n in NAMES), path


def test_module_has_no_cli_entry():
    text = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(text)
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "main" not in defined
    assert '__name__ == "__main__"' not in text
    assert "os.getenv" not in text
