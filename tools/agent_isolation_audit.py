#!/usr/bin/env python3
"""Read-only static audit of agent-pod isolation in a mctl-gitops checkout.

mctlhq/mctl-agents#807. Reads a fixed set of YAML files under
`--gitops-root` (the `platform-gitops` directory) and emits matrix rows
`{primitive, scope, status, severity, source, field, detail, recommendation}`.
No network, no subprocess, no Kubernetes client; nothing is written but
stdout. Env var NAMES are reported, never values. A file that cannot be read
yields `not-observed`, never `absent`.

    python tools/agent_isolation_audit.py --gitops-root <platform-gitops> [--format json|md]

Exit code: 0 on a completed audit whatever it finds, 2 on unreadable input.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

AGENT_IMAGE = "{{workflow.parameters.agent_image}}"
RUNNER_SA = "mctl-agents-runner"
AGENT_LABEL = ("app.kubernetes.io/name", "mctl-agents")
NS = "argo-workflows"

CWFT_GLOB = "argo-workflows/cluster-templates/cwft-mctl-agents-*.yaml"
SA_RBAC = "argo-workflows/config/sa-and-rbac.yaml"
NETPOL = "argo-workflows/config/networkpolicy.yaml"
QUOTA = "argo-workflows/config/local-workdir-quota.yaml"
PROFILE_GLOB = "agent-platform/execution-profiles/*/profile.yaml"
EXTRA_NETPOL_GLOBS = ("bootstrap/templates/**/*netpol*.yaml",)


@dataclass(frozen=True)
class Row:
    primitive: str
    scope: str
    status: str  # enforced|partial|absent|not-observed
    severity: str  # high|medium|low
    source: str
    field: str
    detail: str
    recommendation: str


@dataclass
class Inputs:
    root: Path
    cwfts: dict[str, list[dict[str, Any]]]
    sa_rbac: list[dict[str, Any]] | None
    netpol: list[dict[str, Any]] | None
    quota: list[dict[str, Any]] | None
    profiles: dict[str, list[dict[str, Any]]]
    other_netpols: dict[str, list[dict[str, Any]]]


class UnreadableInput(Exception):
    pass


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        docs = list(yaml.safe_load_all(path.read_text()))
    except (OSError, yaml.YAMLError) as exc:
        raise UnreadableInput(f"{path}: {exc}") from exc
    return [d for d in docs if isinstance(d, dict)]


def _load_opt(path: Path) -> list[dict[str, Any]] | None:
    return _load(path) if path.is_file() else None


def load_inputs(root: Path) -> Inputs:
    cwfts = {str(p.relative_to(root)): _load(p) for p in sorted(root.glob(CWFT_GLOB))}
    profiles = {str(p.relative_to(root)): _load(p) for p in sorted(root.glob(PROFILE_GLOB))}
    others: dict[str, list[dict[str, Any]]] = {}
    for pat in EXTRA_NETPOL_GLOBS:
        for p in sorted(root.glob(pat)):
            docs = [d for d in _load(p) if d.get("kind") == "NetworkPolicy"]
            if docs:
                others[str(p.relative_to(root))] = docs
    return Inputs(
        root, cwfts, _load_opt(root / SA_RBAC), _load_opt(root / NETPOL),
        _load_opt(root / QUOTA), profiles, others,
    )


def classify_templates(cwft_docs: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], bool]]:
    """Return (cwft name, template, is_agent_step) for every template."""
    out: list[tuple[str, dict[str, Any], bool]] = []
    for doc in cwft_docs:
        spec = doc.get("spec") or {}
        name = (doc.get("metadata") or {}).get("name", "?")
        for tpl in spec.get("templates") or []:
            ctr = tpl.get("container") or {}
            out.append((name, tpl, ctr.get("image") == AGENT_IMAGE))
    return out


def _scope(cwft: str, tpl: dict[str, Any]) -> str:
    return f"{cwft}/{tpl.get('name', '?')}"


def _flag(ok: bool) -> str:
    return "enforced" if ok else "partial"


def check_identity(src: str, cwft: str, tpl: dict[str, Any], spec: dict[str, Any],
                   bound_sas: set[str] | None) -> list[Row]:
    scope = _scope(cwft, tpl)
    sa = tpl.get("serviceAccountName")
    rows = [Row("RBAC/ServiceAccount", scope, _flag(sa == RUNNER_SA), "high", src,
                "serviceAccountName", f"serviceAccountName={sa!r}, expected {RUNNER_SA!r}",
                f"Use {RUNNER_SA} on agent steps.")]
    am = tpl.get("automountServiceAccountToken")
    rows.append(Row("RBAC/ServiceAccount", scope, _flag(am is False), "high", src,
                    "automountServiceAccountToken", f"value={am!r}, expected False",
                    "Set automountServiceAccountToken: false."))
    if bound_sas is None:
        rows.append(Row("RBAC/ServiceAccount", scope, "not-observed", "high", SA_RBAC,
                        "RoleBinding", "sa-and-rbac.yaml not readable", "Provide the file."))
    else:
        rows.append(Row("RBAC/ServiceAccount", scope, _flag(RUNNER_SA not in bound_sas), "high",
                        SA_RBAC, "RoleBinding/ClusterRoleBinding subjects",
                        f"{RUNNER_SA} has {'a' if RUNNER_SA in bound_sas else 'no'} binding",
                        f"Keep {RUNNER_SA} binding-free."))
    return rows


def check_pod_security(src: str, cwft: str, tpl: dict[str, Any]) -> list[Row]:
    scope = _scope(cwft, tpl)
    psc = tpl.get("securityContext") or {}
    csc = (tpl.get("container") or {}).get("securityContext") or {}
    drop = [str(x).upper() for x in (csc.get("capabilities") or {}).get("drop") or []]
    checks = [
        ("securityContext.runAsNonRoot",
         psc.get("runAsNonRoot") is True or csc.get("runAsNonRoot") is True, "high"),
        ("securityContext.seccompProfile.type",
         (psc.get("seccompProfile") or {}).get("type") == "RuntimeDefault", "medium"),
        ("container.securityContext.allowPrivilegeEscalation", csc.get("allowPrivilegeEscalation") is False, "high"),
        ("container.securityContext.capabilities.drop", "ALL" in drop, "high"),
    ]
    rows = [Row("PodSecurity", scope, _flag(ok), sev, src, f, f"{f}: {'ok' if ok else 'missing'}",
                "Set the restricted-profile value.") for f, ok, sev in checks]
    ro = csc.get("readOnlyRootFilesystem") is True
    rows.append(Row("PodSecurity", scope, "enforced" if ro else "absent", "medium", src,
                    "container.securityContext.readOnlyRootFilesystem",
                    "readOnlyRootFilesystem " + ("set" if ro else "not set"),
                    "Mount writable emptyDirs and set readOnlyRootFilesystem: true."))
    return rows


def check_runtime_class(src: str, cwft: str, tpl: dict[str, Any], spec: dict[str, Any]) -> Row:
    rc = tpl.get("runtimeClassName") or spec.get("runtimeClassName")
    return Row("SandboxRuntime", _scope(cwft, tpl), "enforced" if rc else "absent", "medium", src,
               "runtimeClassName", f"runtimeClassName={rc!r}",
               "Evaluate a gVisor/Kata RuntimeClass on a labelled node pool.")


def check_resources(src: str, cwft: str, tpl: dict[str, Any]) -> list[Row]:
    scope = _scope(cwft, tpl)
    res = (tpl.get("container") or {}).get("resources") or {}
    have = {k: bool((res.get(k) or {}).get(r)) for k in ("requests", "limits")
            for r in ("cpu", "memory", "ephemeral-storage")}
    missing = sorted(k for k, v in have.items() if not v)
    deadline = tpl.get("activeDeadlineSeconds")
    mutex = bool(tpl.get("synchronization"))
    return [
        Row("Resources", scope, _flag(not missing), "low", src, "container.resources",
            "missing: " + (", ".join(missing) or "none"), "Set cpu/memory/ephemeral requests and limits."),
        Row("Resources", scope, _flag(bool(deadline)), "low", src, "activeDeadlineSeconds",
            f"activeDeadlineSeconds={deadline!r}", "Bound every agent step."),
        Row("Resources", scope, "enforced" if mutex else "absent", "low", src, "synchronization",
            "step mutex " + ("present" if mutex else "absent"), "Bound fan-out with a mutex or semaphore."),
    ]


def check_credentials_env(src: str, cwft: str, tpl: dict[str, Any]) -> Row:
    """Names only. A literal `value:` is never read into the output."""
    env = (tpl.get("container") or {}).get("env") or []
    names = sorted(str(e.get("name")) for e in env
                   if isinstance(e, dict) and (e.get("valueFrom") or {}).get("secretKeyRef"))
    return Row("CredentialExposure", _scope(cwft, tpl), "partial" if names else "enforced",
               "high" if names else "low", src, "container.env[].valueFrom.secretKeyRef",
               "secret env vars readable by the agent shell: " + (", ".join(names) or "none"),
               "Short-lived file-mounted or proxy-injected credentials.")


def _selects_agents(pol: dict[str, Any]) -> bool:
    sel = (pol.get("spec") or {}).get("podSelector")
    if sel is None:
        return False
    labels = sel.get("matchLabels") or {}
    return labels.get(AGENT_LABEL[0]) == AGENT_LABEL[1]


def _has_egress(pol: dict[str, Any]) -> bool:
    return "Egress" in ((pol.get("spec") or {}).get("policyTypes") or [])


def check_egress(netpol: list[dict[str, Any]] | None) -> Row:
    if netpol is None:
        return Row("NetworkPolicy/egress", NS, "not-observed", "high", NETPOL, "spec.policyTypes",
                   "networkpolicy.yaml not readable", "Provide the file.")
    ok = any(d.get("kind") == "NetworkPolicy" and _has_egress(d) and _selects_agents(d) for d in netpol)
    return Row("NetworkPolicy/egress", NS, "enforced" if ok else "absent", "high", NETPOL,
               "spec.policyTypes+podSelector",
               "egress policy selects agent pods" if ok else
               f"no Egress policy selects {AGENT_LABEL[0]}={AGENT_LABEL[1]}",
               "Default-deny egress for agent pods with an FQDN or proxy allowlist.")


def check_east_west(netpol: list[dict[str, Any]] | None, others: dict[str, list[dict[str, Any]]]) -> list[Row]:
    if netpol is None:
        return [Row("NetworkPolicy/east-west", NS, "not-observed", "medium", NETPOL, "spec.podSelector",
                    "networkpolicy.yaml not readable", "Provide the file.")]
    wide = [d for d in netpol if d.get("kind") == "NetworkPolicy"
            and any(any(f.get("podSelector") == {} for f in (r.get("from") or []))
                    for r in (d.get("spec") or {}).get("ingress") or [])]
    rows = [Row("NetworkPolicy/east-west", NS, "partial" if wide else "enforced", "medium", NETPOL,
                "spec.ingress[].from[].podSelector",
                "ingress admits every pod in the namespace (agent -> platform pod reachable)" if wide
                else "no namespace-wide pod ingress", "Refuse agent-to-non-agent pod traffic.")]
    for path, docs in sorted(others.items()):
        for d in docs:
            if any((f.get("namespaceSelector") or {}).get("matchLabels", {}).get(
                    "kubernetes.io/metadata.name") == NS
                   for r in (d.get("spec") or {}).get("ingress") or [] for f in r.get("from") or []):
                rows.append(Row("NetworkPolicy/cross-namespace", (d.get("metadata") or {}).get("name", "?"),
                                "partial", "medium", path, "spec.ingress[].from[].namespaceSelector",
                                f"admits the whole {NS} namespace, agent pods included",
                                "Narrow the selector to non-agent pods."))
    return rows


def check_quota(quota: list[dict[str, Any]] | None) -> Row:
    if quota is None:
        return Row("ResourceQuota", NS, "not-observed", "low", QUOTA, "spec.hard", "file not readable",
                   "Provide the file.")
    keys = {k for d in quota if d.get("kind") == "ResourceQuota"
            for k in ((d.get("spec") or {}).get("hard") or {})}
    compute = sorted(k for k in keys if k in ("pods", "limits.cpu", "limits.memory", "requests.cpu", "requests.memory"))
    return Row("ResourceQuota", NS, "enforced" if compute else "absent", "low", QUOTA, "spec.hard",
               "compute/pod quota keys: " + (", ".join(compute) or "none"),
               "Add pods/limits quota and a LimitRange on the agent namespace.")


def check_profile_network(netpol: list[dict[str, Any]] | None,
                          profiles: dict[str, list[dict[str, Any]]]) -> list[Row]:
    egress = check_egress(netpol)
    rows: list[Row] = []
    for path, docs in sorted(profiles.items()):
        for d in docs:
            net = ((d.get("spec") or {}).get("permissions") or {}).get("network")
            if net in (None, "none", "unrestricted"):
                continue
            name = (d.get("metadata") or {}).get("name", path)
            status = {"absent": "declared-not-enforced", "enforced": "enforced"}.get(egress.status, "not-observed")
            rows.append(Row("NetworkPolicy/profile", name, status, "high", path, "spec.permissions.network",
                            f"profile declares network={net!r}; egress check is {egress.status}",
                            "Back the declaration with an egress NetworkPolicy."))
    return rows


def _bound_sas(sa_rbac: list[dict[str, Any]] | None) -> set[str] | None:
    if sa_rbac is None:
        return None
    return {s.get("name", "") for d in sa_rbac if d.get("kind") in ("RoleBinding", "ClusterRoleBinding")
            for s in d.get("subjects") or []}


def audit(inp: Inputs) -> list[Row]:
    rows: list[Row] = []
    if not inp.cwfts:
        rows.append(Row("PodSecurity", "mctl-agents CWFTs", "not-observed", "high", CWFT_GLOB, "templates",
                        "no agent ClusterWorkflowTemplate found", "Check --gitops-root."))
    bound = _bound_sas(inp.sa_rbac)
    for path, docs in inp.cwfts.items():
        for cwft, tpl, is_agent in classify_templates(docs):
            if not is_agent:
                continue
            spec = next((d.get("spec") or {} for d in docs
                         if (d.get("metadata") or {}).get("name") == cwft), {})
            rows += check_identity(path, cwft, tpl, spec, bound)
            rows += check_pod_security(path, cwft, tpl)
            rows.append(check_runtime_class(path, cwft, tpl, spec))
            rows += check_resources(path, cwft, tpl)
            rows.append(check_credentials_env(path, cwft, tpl))
    rows.append(check_egress(inp.netpol))
    rows += check_east_west(inp.netpol, inp.other_netpols)
    rows.append(check_quota(inp.quota))
    rows += check_profile_network(inp.netpol, inp.profiles)
    return rows


def render(rows: list[Row], inp: Inputs, fmt: str) -> str:
    if fmt == "json":
        classified = {p: [{"cwft": c, "template": t.get("name"), "agent": a}
                          for c, t, a in classify_templates(d)] for p, d in inp.cwfts.items()}
        return json.dumps({"files_read": sorted(inp.cwfts), "templates": classified,
                           "rows": [asdict(r) for r in rows]}, indent=2, sort_keys=True)
    head = "| primitive | scope | status | severity | source | field | detail | recommendation |"
    lines = [head, "|" + "---|" * 8]
    for r in rows:
        cells = [r.primitive, r.scope, r.status, r.severity, r.source, r.field, r.detail, r.recommendation]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gitops-root", required=True, type=Path)
    ap.add_argument("--format", choices=("json", "md"), default="json")
    args = ap.parse_args(argv)
    try:
        inp = load_inputs(args.gitops_root)
    except UnreadableInput as exc:
        print(f"unreadable input: {exc}", file=sys.stderr)
        return 2
    print(render(audit(inp), inp, args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())
