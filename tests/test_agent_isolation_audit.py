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


def _with_resources(res: dict[str, Any]) -> dict[str, Any]:
    cwft = _cwft()
    cwft["spec"]["templates"][0]["container"]["resources"] = res
    return cwft


def _resources_row(tmp_path: Path, res: dict[str, Any]) -> aud.Row:
    rows = _rows(_tree(tmp_path, _with_resources(res), [INGRESS_ONLY]))
    return _find(rows, "Resources", "container.resources")[0]


def test_resources_cpu_memory_without_ephemeral_is_partial(tmp_path: Path) -> None:
    cm = {"cpu": "1", "memory": "1"}
    row = _resources_row(tmp_path, {"requests": cm, "limits": cm})
    assert row.status == "partial"
    assert row.detail == "missing: limits.ephemeral-storage, requests.ephemeral-storage"


def test_resources_only_ephemeral_is_partial_not_enforced(tmp_path: Path) -> None:
    eph = {"ephemeral-storage": "1"}
    row = _resources_row(tmp_path, {"requests": eph, "limits": eph})
    assert row.status == "partial"
    for r in ("cpu", "memory"):
        assert f"requests.{r}" in row.detail and f"limits.{r}" in row.detail


def test_resources_all_set_is_enforced(tmp_path: Path) -> None:
    full = {"cpu": "1", "memory": "1", "ephemeral-storage": "1"}
    row = _resources_row(tmp_path, {"requests": full, "limits": full})
    assert (row.status, row.detail) == ("enforced", "missing: none")


OTHER_LABEL_EGRESS = {"kind": "NetworkPolicy", "metadata": {"name": "o", "namespace": "argo-workflows"},
                      "spec": {"podSelector": {"matchLabels": {"app": "other"}}, "policyTypes": ["Egress"]}}


def test_egress_policy_for_other_label_does_not_count(tmp_path: Path) -> None:
    rows = _rows(_tree(tmp_path, _cwft(), [INGRESS_ONLY, OTHER_LABEL_EGRESS]))
    assert _find(rows, "NetworkPolicy/egress")[0].status == "absent"
    assert [r.status for r in _find(rows, "NetworkPolicy/profile")] == ["declared-not-enforced"]


def test_allow_all_egress_is_partial_not_enforced(tmp_path: Path) -> None:
    allow_all = {**EGRESS, "spec": {**EGRESS["spec"], "egress": [{}]}}
    cidr_all = {"kind": "NetworkPolicy", "metadata": {"name": "c"},
                "spec": {**EGRESS["spec"], "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}]}}
    for i, pol in enumerate((allow_all, cidr_all)):
        root = _tree(tmp_path / str(i), _cwft(), [INGRESS_ONLY, pol])
        rows = _rows(root)
        egress = _find(rows, "NetworkPolicy/egress")[0]
        assert egress.status == "partial" and "allow-all" in egress.detail
        assert [r.status for r in _find(rows, "NetworkPolicy/profile")] == ["declared-not-enforced"]


def test_namespace_wide_default_deny_selects_agents(tmp_path: Path) -> None:
    deny = {"kind": "NetworkPolicy", "metadata": {"name": "deny", "namespace": "argo-workflows"},
            "spec": {"podSelector": {}, "policyTypes": ["Egress"]}}
    rows = _rows(_tree(tmp_path, _cwft(), [INGRESS_ONLY, deny]))
    assert _find(rows, "NetworkPolicy/egress")[0].status == "enforced"


def test_egress_policy_in_other_namespace_does_not_count(tmp_path: Path) -> None:
    foreign = {**EGRESS, "metadata": {"name": "e", "namespace": "vault"}}
    rows = _rows(_tree(tmp_path, _cwft(), [INGRESS_ONLY, foreign]))
    assert _find(rows, "NetworkPolicy/egress")[0].status == "absent"


def test_egress_policy_outside_netpol_file_is_scanned(tmp_path: Path) -> None:
    root = _tree(tmp_path, _cwft(), [INGRESS_ONLY])
    deny = {"kind": "NetworkPolicy", "metadata": {"name": "d", "namespace": "argo-workflows"},
            "spec": {"podSelector": {}, "policyTypes": ["Egress"]}}
    p = root / "infra/agents/egress.yml"
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump(deny))
    assert _find(_rows(root), "NetworkPolicy/egress")[0].status == "enforced"


def test_cross_namespace_policy_found_by_kind_not_filename(tmp_path: Path) -> None:
    root = _tree(tmp_path, _cwft(), [INGRESS_ONLY])
    admit = {"kind": "NetworkPolicy", "metadata": {"name": "vault-in", "namespace": "vault"},
             "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"from": [
                 {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "argo-workflows"}}}]}]}}
    p = root / "infra-components/identity/vault/policies.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump(admit))
    (root / "helm/templates").mkdir(parents=True)
    (root / "helm/templates/np.yaml").write_text(
        "{{- if .Values.on }}\nkind: NetworkPolicy\nmetadata:\n  namespace: {{ .Release.Namespace }}\n{{- end }}\n")
    rows = _find(_rows(root), "NetworkPolicy/cross-namespace")
    assert [(r.scope, r.status) for r in rows if r.source.endswith("policies.yaml")] == [("vault-in", "partial")]
    assert [r.status for r in rows if r.source == "helm/templates/np.yaml"] == ["not-observed"]


def test_templated_file_naming_agent_ns_makes_egress_not_observed(tmp_path: Path) -> None:
    root = _tree(tmp_path, _cwft(), [INGRESS_ONLY])
    (root / "t").mkdir()
    (root / "t/np.yaml").write_text("{{- if .Values.on }}\nkind: NetworkPolicy\nmetadata:\n"
                                    "  namespace: argo-workflows\n{{- end }}\n")
    rows = _rows(root)
    assert _find(rows, "NetworkPolicy/egress")[0].status == "not-observed"
    assert [r.status for r in _find(rows, "NetworkPolicy/profile")] == ["not-observed"]


def test_cwfts_without_agent_template_emit_not_observed(tmp_path: Path) -> None:
    cwft = _cwft()
    cwft["spec"]["templates"][0]["container"]["image"] = "{{workflow.parameters.renamed_image}}"
    rows = _rows(_tree(tmp_path, cwft, [INGRESS_ONLY]))
    miss = [r for r in rows if r.scope == "mctl-agents CWFTs"]
    assert [(r.status, r.severity) for r in miss] == [("not-observed", "high")]
    assert "no template runs image" in miss[0].detail


def test_script_agent_template_is_audited(tmp_path: Path) -> None:
    cwft = _cwft()
    tpl = cwft["spec"]["templates"][0]
    tpl["script"] = {**tpl.pop("container"), "source": "true"}
    rows = _rows(_tree(tmp_path, cwft, [INGRESS_ONLY]))
    assert [r.status for r in _find(rows, "PodSecurity", "capabilities.drop")] == ["enforced"]
    assert not [r for r in rows if r.scope == "mctl-agents CWFTs"]


def test_spec_level_defaults_are_used(tmp_path: Path) -> None:
    cwft = _cwft()
    spec, tpl = cwft["spec"], cwft["spec"]["templates"][0]
    spec["activeDeadlineSeconds"] = tpl.pop("activeDeadlineSeconds")
    spec["securityContext"] = tpl.pop("securityContext")
    spec["synchronization"] = {"mutex": {"name": "m"}}
    spec["automountServiceAccountToken"] = tpl.pop("automountServiceAccountToken")
    rows = _rows(_tree(tmp_path, cwft, [INGRESS_ONLY]))
    dl = _find(rows, "Resources", "activeDeadlineSeconds")[0]
    assert dl.status == "enforced" and "(spec)" in dl.detail
    assert _find(rows, "Resources", "synchronization")[0].status == "enforced"
    assert _find(rows, "PodSecurity", "seccompProfile")[0].status == "enforced"
    assert _find(rows, "RBAC/ServiceAccount", "automount")[0].status == "enforced"


def test_template_overrides_spec_level(tmp_path: Path) -> None:
    cwft = _cwft()
    cwft["spec"]["serviceAccountName"] = "argo-workflow-sa"
    sa = _find(_rows(_tree(tmp_path, cwft, [INGRESS_ONLY])), "RBAC/ServiceAccount", "serviceAccountName")[0]
    assert sa.status == "enforced" and "(template)" in sa.detail


def test_non_utf8_core_file_exit_2(tmp_path: Path) -> None:
    root = _tree(tmp_path, _cwft(), [INGRESS_ONLY])
    (root / "argo-workflows/config/sa-and-rbac.yaml").write_bytes(b"kind: \xff\xfe\n")
    assert aud.main(["--gitops-root", str(root)]) == 2


def test_missing_gitops_root_exit_2(tmp_path: Path) -> None:
    assert aud.main(["--gitops-root", str(tmp_path / "nope")]) == 2


def test_quota_and_markdown_render(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, _cwft(), [INGRESS_ONLY])
    assert _find(_rows(root), "ResourceQuota")[0].status == "absent"
    assert aud.main(["--gitops-root", str(root), "--format", "md"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("| primitive | scope | status |")
    assert all(ln.startswith("|") and ln.count(" | ") >= 7 for ln in out[2:])
