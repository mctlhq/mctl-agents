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


_AW_NS = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "argo-workflows"}}}
_AGENT_POD = {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "mctl-agents"}}}


def _allow_agent_ingress(*peers: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "NetworkPolicy",
            "metadata": {"name": "allow-agent-ingress", "namespace": "mctl-agents-egress"},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"],
                     "ingress": [{"from": list(peers), "ports": [{"port": 3128, "protocol": "TCP"}]}]}}


def _xns(tmp_path: Path, *peers: dict[str, Any], ns: str = "mctl-agents-egress") -> list[aud.Row]:
    root = _tree(tmp_path, _cwft(), [INGRESS_ONLY])
    pol = _allow_agent_ingress(*peers)
    pol["metadata"]["namespace"] = ns
    p = root / "infra/egress/networkpolicy.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump(pol))
    return _find(_rows(root), "NetworkPolicy/cross-namespace")


def test_combined_peer_is_agents_only_enforced(tmp_path: Path) -> None:
    rows = _xns(tmp_path, {**_AW_NS, **_AGENT_POD})
    assert [(r.status, r.severity) for r in rows] == [("enforced", "low")]
    assert "only agent pods" in rows[0].detail


def test_podselector_removed_is_partial(tmp_path: Path) -> None:
    rows = _xns(tmp_path, dict(_AW_NS))
    assert [r.status for r in rows] == ["partial"]
    assert "whole argo-workflows namespace" in rows[0].detail


def test_pod_only_peer_in_other_namespace_no_row(tmp_path: Path) -> None:
    assert _xns(tmp_path, dict(_AGENT_POD), ns="vault") == []


def test_or_across_peers(tmp_path: Path) -> None:
    other = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "other"}}}
    assert [r.status for r in _xns(tmp_path / "a", {**_AW_NS, **_AGENT_POD}, dict(_AW_NS))] == ["partial"]
    assert [r.status for r in _xns(tmp_path / "b", {**_AW_NS, **_AGENT_POD}, other)] == ["enforced"]


def test_empty_selectors_are_partial(tmp_path: Path) -> None:
    cases = [{"namespaceSelector": {}}, {"namespaceSelector": {}, "podSelector": {}},
             {**_AW_NS, "podSelector": {}}]
    for i, peer in enumerate(cases):
        (tmp_path / str(i)).mkdir()
        assert [r.status for r in _xns(tmp_path / str(i), peer)] == ["partial"]


def test_undecidable_peers_not_observed(tmp_path: Path) -> None:
    cases = [{"namespaceSelector": {"matchExpressions": [{"key": "a", "operator": "Exists"}]}},
             {**_AW_NS, "podSelector": {"matchLabels": {"app": "other"}}},
             {**_AW_NS, "podSelector": {"matchExpressions": [{"key": "a", "operator": "Exists"}]}}]
    for i, peer in enumerate(cases):
        (tmp_path / str(i)).mkdir()
        assert [r.status for r in _xns(tmp_path / str(i), peer)] == ["not-observed"]


# --- #954: namespaceSelector={} is every namespace, not argo-workflows -------------------------

_BROAD = {"namespaceSelector": {}, **_AGENT_POD}


def _ew(*peers: dict[str, Any], ns: str | None = "mctl-agents-egress") -> list[aud.Row]:
    """check_east_west with a clean in-namespace policy and one cross-namespace policy."""
    pol = _allow_agent_ingress(*peers)
    if ns is None:
        del pol["metadata"]["namespace"]
    else:
        pol["metadata"]["namespace"] = ns
    return aud.check_east_west([], {"infra/np.yaml": [pol]})


def test_954_broad_namespace_is_partial_not_enforced_and_makes_no_namespace_claim() -> None:
    rows = _ew(dict(_BROAD))
    assert [(r.primitive, r.status) for r in rows] == [
        ("NetworkPolicy/east-west", "enforced"), ("NetworkPolicy/cross-namespace", "partial")]
    row = rows[1]
    assert (row.scope, row.severity, row.source) == ("allow-agent-ingress", "medium", "infra/np.yaml")
    assert row.field == "spec.ingress[].from[].namespaceSelector+podSelector"
    assert "every namespace" in row.detail and "any namespace" in row.detail
    assert "only agent pods" not in row.detail
    assert "argo-workflows" in row.recommendation


def test_954_restricted_argo_workflows_stays_enforced_with_namespace_claim() -> None:
    rows = _ew({**_AW_NS, **_AGENT_POD})
    assert [(r.primitive, r.status, r.severity) for r in rows[1:]] == [
        ("NetworkPolicy/cross-namespace", "enforced", "low")]
    assert rows[1].detail == "admits only agent pods (app.kubernetes.io/name=mctl-agents) from argo-workflows"


def test_954_omitted_namespace_selector_resolves_to_the_policy_namespace() -> None:
    assert [r.status for r in _ew(dict(_AGENT_POD), ns="argo-workflows")[1:]] == ["enforced"]
    assert _ew(dict(_AGENT_POD), ns="vault")[1:] == []
    assert [r.status for r in _ew(dict(_AGENT_POD), ns=None)[1:]] == ["not-observed"]


def test_954_unsupported_selectors_stay_not_observed() -> None:
    exprs = {"matchExpressions": [{"key": "a", "operator": "Exists"}]}
    cases = [{"namespaceSelector": exprs, **_AGENT_POD},
             {"namespaceSelector": {}, "podSelector": {"matchLabels": {"app": "other"}}},
             {"namespaceSelector": {}, "podSelector": exprs},
             {"namespaceSelector": {"matchLabels": {"team": "x"}}, **_AGENT_POD}]
    for peer in cases:
        assert [r.status for r in _ew(peer)[1:]] == ["not-observed"], peer


def test_954_unrestricted_selections_are_partial_with_their_own_detail() -> None:
    for peer in ({"namespaceSelector": {}}, {"namespaceSelector": {}, "podSelector": {}},
                 {**_AW_NS, "podSelector": {}}):
        rows = _ew(peer)[1:]
        assert [r.status for r in rows] == ["partial"], peer
        assert "whole argo-workflows namespace" in rows[0].detail


def test_954_narrow_or_broad_is_broad_and_and_is_kept_inside_a_peer() -> None:
    narrow = {**_AW_NS, **_AGENT_POD}
    both = _ew(narrow, dict(_BROAD))[1:]
    assert [r.status for r in both] == ["partial"] and "every namespace" in both[0].detail
    assert [r.status for r in _ew(dict(_BROAD), narrow)[1:]] == ["partial"]
    # A known broad peer is not hidden by an undecidable sibling, and vice versa.
    undecidable = {"namespaceSelector": {"matchLabels": {"team": "x"}}}
    assert [r.status for r in _ew(narrow, undecidable)[1:]] == ["not-observed"]
    assert [r.status for r in _ew(dict(_BROAD), undecidable)[1:]] == ["partial"]
    # Separate rules are ORed too.
    pol = _allow_agent_ingress(narrow)
    pol["metadata"]["namespace"] = "x"
    pol["spec"]["ingress"].append({"from": [dict(_BROAD)]})
    assert [r.status for r in aud.check_east_west([], {"p": [pol]})[1:]] == ["partial"]
    # AND, not OR, inside one peer: namespace {} plus a non-agent pod is not "agents".
    assert [r.status for r in _ew({"namespaceSelector": {}, "podSelector": {"matchLabels": {"a": "b"}}})[1:]] \
        == ["not-observed"]


def test_954_end_to_end_json_row(tmp_path: Path) -> None:
    rows = _xns(tmp_path, dict(_BROAD))
    assert [(r.status, r.severity) for r in rows] == [("partial", "medium")]


def test_json_schema_version(tmp_path: Path) -> None:
    inp = aud.load_inputs(_tree(tmp_path, _cwft(), [INGRESS_ONLY]))
    out = json.loads(aud.render(aud.audit(inp), inp, "json"))
    assert out["schema_version"] == 2


# --- container securityContext overrides the pod one (Kubernetes precedence) ---

def _pod_security(tmp_path: Path, csc_patch: dict[str, Any], psc_patch: dict[str, Any] | None = None
                  ) -> dict[str, str]:
    cwft = _cwft()
    tpl = cwft["spec"]["templates"][0]
    tpl["container"]["securityContext"].update(csc_patch)
    if psc_patch is not None:
        tpl["securityContext"] = psc_patch
    rows = _find(_rows(_tree(tmp_path, cwft, [INGRESS_ONLY])), "PodSecurity")
    return {r.field: r.status for r in rows}


def test_container_run_as_non_root_false_overrides_pod_true(tmp_path: Path) -> None:
    got = _pod_security(tmp_path, {"runAsNonRoot": False})
    assert got["securityContext.runAsNonRoot"] == "partial"


def test_container_run_as_non_root_true_with_pod_unset_is_enforced(tmp_path: Path) -> None:
    got = _pod_security(tmp_path, {"runAsNonRoot": True}, {"seccompProfile": {"type": "RuntimeDefault"}})
    assert got["securityContext.runAsNonRoot"] == "enforced"


def test_pod_run_as_non_root_applies_when_container_omits_it(tmp_path: Path) -> None:
    cwft = _cwft()
    del cwft["spec"]["templates"][0]["container"]["securityContext"]["runAsNonRoot"]
    rows = _find(_rows(_tree(tmp_path, cwft, [INGRESS_ONLY])), "PodSecurity", "runAsNonRoot")
    assert [r.status for r in rows] == ["enforced"]


def test_container_seccomp_unconfined_overrides_pod_runtime_default(tmp_path: Path) -> None:
    got = _pod_security(tmp_path, {"seccompProfile": {"type": "Unconfined"}})
    assert got["securityContext.seccompProfile.type"] == "partial"


def test_container_seccomp_runtime_default_with_pod_unset_is_enforced(tmp_path: Path) -> None:
    got = _pod_security(tmp_path, {"seccompProfile": {"type": "RuntimeDefault"}}, {"runAsNonRoot": True})
    assert got["securityContext.seccompProfile.type"] == "enforced"


# --- policyTypes defaulting: an egress section without policyTypes is an egress policy ---

def test_egress_section_without_policy_types_counts_as_egress_policy(tmp_path: Path) -> None:
    pol = {"kind": "NetworkPolicy", "metadata": {"name": "e-defaulted"},
           "spec": {"podSelector": {}, "egress": [{"to": [{"podSelector": {"matchLabels": {"app": "dns"}}}]}]}}
    rows = _rows(_tree(tmp_path, _cwft(), [INGRESS_ONLY, pol]))
    assert _find(rows, "NetworkPolicy/egress")[0].status == "enforced"
    assert [r.status for r in _find(rows, "NetworkPolicy/profile")] == ["enforced"]


def test_open_egress_section_without_policy_types_is_partial(tmp_path: Path) -> None:
    pol = {"kind": "NetworkPolicy", "metadata": {"name": "e-open"},
           "spec": {"podSelector": {}, "egress": [{}]}}
    rows = _rows(_tree(tmp_path, _cwft(), [INGRESS_ONLY, pol]))
    assert _find(rows, "NetworkPolicy/egress")[0].status == "partial"


def test_empty_egress_section_without_policy_types_is_ingress_only(tmp_path: Path) -> None:
    # The API server adds Egress only for a non-empty egress section.
    pol = {"kind": "NetworkPolicy", "metadata": {"name": "e-empty"}, "spec": {"podSelector": {}, "egress": []}}
    rows = _rows(_tree(tmp_path, _cwft(), [INGRESS_ONLY, pol]))
    assert _find(rows, "NetworkPolicy/egress")[0].status == "absent"


def test_explicit_policy_types_are_not_overridden_by_egress_section(tmp_path: Path) -> None:
    pol = {"kind": "NetworkPolicy", "metadata": {"name": "i-only"},
           "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "egress": [{"to": [{"podSelector": {}}]}]}}
    rows = _rows(_tree(tmp_path, _cwft(), [INGRESS_ONLY, pol]))
    assert _find(rows, "NetworkPolicy/egress")[0].status == "absent"


# --- an unreadable non-core path is not-observed, never exit 2 and never absent ---

def _unreadable(p: Path) -> None:
    p.chmod(0)
    try:
        p.read_bytes() if p.is_file() else list(p.iterdir())
    except OSError:
        return
    pytest.skip("running as a user that can read mode-0 files")


def test_unreadable_non_core_yaml_is_not_observed_not_exit_2(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, _cwft(), [INGRESS_ONLY])
    other = root / "elsewhere/np.yaml"
    other.parent.mkdir()
    other.write_text(yaml.safe_dump(EGRESS))
    _unreadable(other)
    try:
        assert aud.main(["--gitops-root", str(root)]) == 0
        rows = json.loads(capsys.readouterr().out)["rows"]
    finally:
        other.chmod(0o644)
    egress = [r for r in rows if r["primitive"] == "NetworkPolicy/egress"]
    assert [r["status"] for r in egress] == ["not-observed"]
    assert "elsewhere/np.yaml" in egress[0]["detail"]
    cross = [r for r in rows if r["primitive"] == "NetworkPolicy/cross-namespace"
             and r["source"] == "elsewhere/np.yaml"]
    assert [r["status"] for r in cross] == ["not-observed"]
    assert [r["status"] for r in rows if r["primitive"] == "NetworkPolicy/profile"] == ["not-observed"]


def test_unlistable_directory_is_not_observed(tmp_path: Path) -> None:
    root = _tree(tmp_path, _cwft(), [INGRESS_ONLY])
    hidden = root / "locked"
    hidden.mkdir()
    (hidden / "np.yaml").write_text(yaml.safe_dump(EGRESS))
    _unreadable(hidden)
    try:
        rows = _rows(root)
    finally:
        hidden.chmod(0o755)
    egress = _find(rows, "NetworkPolicy/egress")[0]
    assert egress.status == "not-observed"
    assert "locked" in egress.detail


def test_unreadable_core_file_still_exits_2(tmp_path: Path) -> None:
    root = _tree(tmp_path, _cwft(), [INGRESS_ONLY])
    core = root / "argo-workflows/cluster-templates/cwft-mctl-agents-x.yaml"
    _unreadable(core)
    try:
        assert aud.main(["--gitops-root", str(root)]) == 2
    finally:
        core.chmod(0o644)
