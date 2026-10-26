"""Tests for tools/agent_isolation_audit.py (#807). Fixture trees are built in tmp_path."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools import agent_isolation_audit as aud

IMG = "{{workflow.parameters.agent_image}}"


def _cwft(*, drop: bool = True, sa: str = "mctl-agents-runner", secret_value: str | None = None) -> dict[str, Any]:
    env: list[dict[str, Any]] = [
        {"name": "GITHUB_TOKEN", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]
    if secret_value:
        env.append({"name": "LITERAL_SECRET", "value": secret_value})
    csc: dict[str, Any] = {"runAsNonRoot": True, "allowPrivilegeEscalation": False}
    if drop:
        csc["capabilities"] = {"drop": ["ALL"]}
    return {"kind": "ClusterWorkflowTemplate", "metadata": {"name": "mctl-agents-x"}, "spec": {"templates": [
        {"name": "agent", "serviceAccountName": sa, "automountServiceAccountToken": False,
         "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
         "activeDeadlineSeconds": 60,
         "container": {"image": IMG, "securityContext": csc, "env": env,
                       "resources": {"requests": {"cpu": "1", "memory": "1", "ephemeral-storage": "1"},
                                     "limits": {"cpu": "1", "memory": "1", "ephemeral-storage": "1"}}}},
        {"name": "helper", "container": {"image": "alpine"}}]}}


INGRESS_ONLY = {"kind": "NetworkPolicy", "metadata": {"name": "i"},
                "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"from": [{"podSelector": {}}]}]}}
EGRESS = {"kind": "NetworkPolicy", "metadata": {"name": "e"},
          "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "mctl-agents"}},
                   "policyTypes": ["Egress"]}}
PROFILE = {"kind": "ExecutionProfile", "metadata": {"name": "p"},
           "spec": {"permissions": {"network": "approved-providers-only"}}}
SA = [{"kind": "ServiceAccount", "metadata": {"name": "mctl-agents-runner"}}]


def _tree(tmp: Path, cwft: dict[str, Any], netpols: list[dict[str, Any]] | None = None) -> Path:
    def w(rel: str, docs: list[dict[str, Any]]) -> None:
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump_all(docs))

    w("argo-workflows/cluster-templates/cwft-mctl-agents-x.yaml", [cwft])
    w("argo-workflows/config/sa-and-rbac.yaml", SA)
    quota = {"kind": "ResourceQuota", "spec": {"hard": {"requests.storage": "1"}}}
    w("argo-workflows/config/local-workdir-quota.yaml", [quota])
    w("agent-platform/execution-profiles/p/profile.yaml", [PROFILE])
    if netpols is not None:
        w("argo-workflows/config/networkpolicy.yaml", netpols)
    return tmp


def _rows(root: Path) -> list[aud.Row]:
    return aud.audit(aud.load_inputs(root))


def _find(rows: list[aud.Row], primitive: str, field: str = "") -> list[aud.Row]:
    return [r for r in rows if r.primitive == primitive and field in r.field]


def test_t1_compliant_template(tmp_path: Path) -> None:
    rows = _rows(_tree(tmp_path, _cwft(), [INGRESS_ONLY]))
    assert all(r.scope.endswith("/agent") for r in rows if r.primitive in ("PodSecurity", "SandboxRuntime"))
    for f in ("runAsNonRoot", "seccompProfile", "allowPrivilegeEscalation", "capabilities.drop"):
        assert [r.status for r in _find(rows, "PodSecurity", f)] == ["enforced"]
    assert [r.status for r in _find(rows, "RBAC/ServiceAccount", "serviceAccountName")] == ["enforced"]
    assert _find(rows, "SandboxRuntime")[0].status == "absent"
    assert _find(rows, "PodSecurity", "readOnlyRootFilesystem")[0].status == "absent"


def test_t2_ingress_only_egress_absent_and_declared_not_enforced(tmp_path: Path) -> None:
    rows = _rows(_tree(tmp_path, _cwft(), [INGRESS_ONLY]))
    egress = _find(rows, "NetworkPolicy/egress")[0]
    assert (egress.status, egress.severity) == ("absent", "high")
    assert [r.status for r in _find(rows, "NetworkPolicy/profile")] == ["declared-not-enforced"]


def test_t3_egress_policy_flips(tmp_path: Path) -> None:
    rows = _rows(_tree(tmp_path, _cwft(), [INGRESS_ONLY, EGRESS]))
    assert _find(rows, "NetworkPolicy/egress")[0].status == "enforced"
    assert [r.status for r in _find(rows, "NetworkPolicy/profile")] == ["enforced"]


def test_t4_missing_drop_and_wrong_sa(tmp_path: Path) -> None:
    rows = _rows(_tree(tmp_path, _cwft(drop=False, sa="argo-workflow-sa"), [INGRESS_ONLY]))
    drop = _find(rows, "PodSecurity", "capabilities.drop")[0]
    assert drop.status == "partial" and drop.scope == "mctl-agents-x/agent"
    sa = _find(rows, "RBAC/ServiceAccount", "serviceAccountName")[0]
    assert sa.status == "partial" and "argo-workflow-sa" in sa.detail


def test_t5_missing_netpol_is_not_observed(tmp_path: Path) -> None:
    rows = _rows(_tree(tmp_path, _cwft(), None))
    assert _find(rows, "NetworkPolicy/egress")[0].status == "not-observed"
    assert _find(rows, "NetworkPolicy/east-west")[0].status == "not-observed"
    assert [r.status for r in _find(rows, "NetworkPolicy/profile")] == ["not-observed"]


def test_t6_credentials_names_only(tmp_path: Path) -> None:
    literal = "hunter2-very-secret"
    rows = _rows(_tree(tmp_path, _cwft(secret_value=literal), [INGRESS_ONLY]))
    out = aud.render(rows, aud.load_inputs(tmp_path), "json")
    assert "GITHUB_TOKEN" in out
    assert "hunter2" not in out and "LITERAL_SECRET" not in out


def test_t7_pure_and_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, _cwft(), [INGRESS_ONLY])

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("forbidden")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    outs = []
    for _ in range(2):
        assert aud.main(["--gitops-root", str(root)]) == 0
        outs.append(capsys.readouterr().out)
    assert outs[0] == outs[1]
    assert json.loads(outs[0])["rows"]


def test_unreadable_input_exit_2(tmp_path: Path) -> None:
    root = _tree(tmp_path, _cwft(), [INGRESS_ONLY])
    (root / "argo-workflows/config/sa-and-rbac.yaml").write_text("a: [unclosed")
    assert aud.main(["--gitops-root", str(root)]) == 2
