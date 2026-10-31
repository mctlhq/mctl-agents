#!/usr/bin/env python3
"""Read-only static audit of agent-pod isolation in a mctl-gitops checkout.

mctlhq/mctl-agents#807. Reads a fixed set of YAML files under
`--gitops-root` (the `platform-gitops` directory) and emits matrix rows
`{primitive, scope, status, severity, source, field, detail, recommendation}`.
No network, no subprocess, no Kubernetes client; nothing is written but
stdout. Env var NAMES are reported, never values. A file that cannot be read
yields `not-observed`, never `absent`.

    python tools/agent_isolation_audit.py --gitops-root <platform-gitops> [--format json|md]

Every `*.yaml`/`*.yml` file under the root that declares a top-level
`kind: NetworkPolicy` is scanned, so cross-namespace policies are found
wherever they live. A scanned file that does not parse as plain YAML (a Helm
or Go template) yields a `not-observed` row, never silence.

JSON output carries a top-level integer `schema_version`. Version 2 means
ingress peers are evaluated as the intersection of `namespaceSelector` and
`podSelector` (peers in a `from` list are a union). Unversioned output is v1.

Exit code: 0 on a completed audit whatever it finds, 2 on unreadable input
(a missing root, an unreadable or non-UTF-8 core file, invalid YAML in a core file).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

AGENT_IMAGE = "{{workflow.parameters.agent_image}}"
RUNNER_SA = "mctl-agents-runner"
AGENT_LABEL = ("app.kubernetes.io/name", "mctl-agents")
NS = "argo-workflows"
SCHEMA_VERSION = 2

CWFT_GLOB = "argo-workflows/cluster-templates/cwft-mctl-agents-*.yaml"
SA_RBAC = "argo-workflows/config/sa-and-rbac.yaml"
NETPOL = "argo-workflows/config/networkpolicy.yaml"
QUOTA = "argo-workflows/config/local-workdir-quota.yaml"
PROFILE_GLOB = "agent-platform/execution-profiles/*/profile.yaml"
YAML_SUFFIXES = (".yaml", ".yml")
NETPOL_KIND_RE = re.compile(rb"^kind:[ \t]*NetworkPolicy[ \t]*$", re.MULTILINE)
AGENT_NS_RE = re.compile(rb"namespace:[ \t]*[\"']?argo-workflows\b")
COMPUTE_RESOURCES = ("cpu", "memory", "ephemeral-storage")
ALLOW_ALL_CIDRS = ("0.0.0.0/0", "::/0")


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
    unparsed_netpols: dict[str, bool] = field(default_factory=dict)  # path -> names argo-workflows ns


class UnreadableInput(Exception):
    pass


def _parse(raw: bytes) -> list[dict[str, Any]]:
    docs = list(yaml.safe_load_all(raw.decode("utf-8")))
    return [d for d in docs if isinstance(d, dict)]


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise UnreadableInput(f"{path}: {exc}") from exc


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        return _parse(_read(path))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise UnreadableInput(f"{path}: {exc}") from exc


def _load_opt(path: Path) -> list[dict[str, Any]] | None:
    return _load(path) if path.is_file() else None


def _scan_netpols(root: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, bool]]:
    """Every file outside NETPOL with a top-level `kind: NetworkPolicy`.

    Returns parsed NetworkPolicy docs per path, and the paths that could not be
    parsed (templated YAML), each flagged with whether its raw text names the
    agent namespace.
    """
    parsed: dict[str, list[dict[str, Any]]] = {}
    unparsed: dict[str, bool] = {}
    for p in sorted(root.rglob("*")):
        if p.suffix not in YAML_SUFFIXES or not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if rel == NETPOL:
            continue
        raw = _read(p)
        if not NETPOL_KIND_RE.search(raw):
            continue
        try:
            docs = [d for d in _parse(raw) if d.get("kind") == "NetworkPolicy"]
        except (UnicodeDecodeError, yaml.YAMLError):
            unparsed[rel] = bool(AGENT_NS_RE.search(raw))
            continue
        if docs:
            parsed[rel] = docs
    return parsed, unparsed


def load_inputs(root: Path) -> Inputs:
    if not root.is_dir():
        raise UnreadableInput(f"{root}: --gitops-root is not a directory")
    cwfts = {str(p.relative_to(root)): _load(p) for p in sorted(root.glob(CWFT_GLOB))}
    profiles = {str(p.relative_to(root)): _load(p) for p in sorted(root.glob(PROFILE_GLOB))}
    others, unparsed = _scan_netpols(root)
    return Inputs(
        root, cwfts, _load_opt(root / SA_RBAC), _load_opt(root / NETPOL),
        _load_opt(root / QUOTA), profiles, others, unparsed,
    )


def _workflow_spec(doc: dict[str, Any]) -> dict[str, Any]:
    """The Workflow spec of a (Cluster)WorkflowTemplate or a CronWorkflow."""
    spec = doc.get("spec") or {}
    return spec.get("workflowSpec") or spec


def _ctr(tpl: dict[str, Any]) -> dict[str, Any]:
    return tpl.get("container") or tpl.get("script") or {}


def _is_agent_image(image: Any) -> bool:
    return isinstance(image, str) and re.sub(r"\s+", "", image) == AGENT_IMAGE


def classify_templates(
    cwft_docs: list[dict[str, Any]],
) -> list[tuple[str, dict[str, Any], bool, dict[str, Any]]]:
    """Return (cwft name, template, is_agent_step, workflow spec) for every template."""
    out: list[tuple[str, dict[str, Any], bool, dict[str, Any]]] = []
    for doc in cwft_docs:
        spec = _workflow_spec(doc)
        name = (doc.get("metadata") or {}).get("name", "?")
        for tpl in spec.get("templates") or []:
            out.append((name, tpl, _is_agent_image(_ctr(tpl).get("image")), spec))
    return out


def _eff(tpl: dict[str, Any], spec: dict[str, Any], key: str) -> tuple[Any, str]:
    """Template value, else the spec-level default Argo applies. Returns (value, level)."""
    if key in tpl:
        return tpl[key], "template"
    if key in spec:
        return spec[key], "spec"
    return None, "unset"


def _scope(cwft: str, tpl: dict[str, Any]) -> str:
    return f"{cwft}/{tpl.get('name', '?')}"


def _flag(ok: bool) -> str:
    return "enforced" if ok else "partial"


def check_identity(src: str, cwft: str, tpl: dict[str, Any], spec: dict[str, Any],
                   bound_sas: set[str] | None) -> list[Row]:
    scope = _scope(cwft, tpl)
    sa, sa_lvl = _eff(tpl, spec, "serviceAccountName")
    rows = [Row("RBAC/ServiceAccount", scope, _flag(sa == RUNNER_SA), "high", src,
                "serviceAccountName",
                f"serviceAccountName={sa!r} ({sa_lvl}), expected {RUNNER_SA!r}",
                f"Use {RUNNER_SA} on agent steps.")]
    am, am_lvl = _eff(tpl, spec, "automountServiceAccountToken")
    rows.append(Row("RBAC/ServiceAccount", scope, _flag(am is False), "high", src,
                    "automountServiceAccountToken", f"value={am!r} ({am_lvl}), expected False",
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


def check_pod_security(src: str, cwft: str, tpl: dict[str, Any],
                       spec: dict[str, Any] | None = None) -> list[Row]:
    scope = _scope(cwft, tpl)
    # The workflow-level securityContext is the pod default; a template-level one overrides it.
    psc = {**((spec or {}).get("securityContext") or {}), **(tpl.get("securityContext") or {})}
    csc = _ctr(tpl).get("securityContext") or {}
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


def check_resources(src: str, cwft: str, tpl: dict[str, Any],
                    spec: dict[str, Any] | None = None) -> list[Row]:
    scope = _scope(cwft, tpl)
    spec = spec or {}
    res = _ctr(tpl).get("resources") or {}
    have = {f"{kind}.{r}": bool((res.get(kind) or {}).get(r))
            for kind in ("requests", "limits") for r in COMPUTE_RESOURCES}
    missing = sorted(k for k, v in have.items() if not v)
    deadline, dl_lvl = _eff(tpl, spec, "activeDeadlineSeconds")
    sync, sync_lvl = _eff(tpl, spec, "synchronization")
    mutex = bool(sync)
    return [
        Row("Resources", scope, _flag(not missing), "low", src, "container.resources",
            "missing: " + (", ".join(missing) or "none"), "Set cpu/memory/ephemeral requests and limits."),
        Row("Resources", scope, _flag(bool(deadline)), "low", src, "activeDeadlineSeconds",
            f"activeDeadlineSeconds={deadline!r} ({dl_lvl})", "Bound every agent step."),
        Row("Resources", scope, "enforced" if mutex else "absent", "low", src, "synchronization",
            f"mutex/semaphore {'present' if mutex else 'absent'} ({sync_lvl})",
            "Bound fan-out with a mutex or semaphore."),
    ]


def check_credentials_env(src: str, cwft: str, tpl: dict[str, Any]) -> Row:
    """Names only. A literal `value:` is never read into the output."""
    env = _ctr(tpl).get("env") or []
    names = sorted(str(e.get("name")) for e in env
                   if isinstance(e, dict) and (e.get("valueFrom") or {}).get("secretKeyRef"))
    return Row("CredentialExposure", _scope(cwft, tpl), "partial" if names else "enforced",
               "high" if names else "low", src, "container.env[].valueFrom.secretKeyRef",
               "secret env vars readable by the agent shell: " + (", ".join(names) or "none"),
               "Short-lived file-mounted or proxy-injected credentials.")


def _selects_agents(pol: dict[str, Any]) -> bool:
    """True when the podSelector provably selects agent pods.

    An empty selector selects every pod in the namespace. A selector is only
    counted when all its matchLabels are the agent label and it has no
    matchExpressions: other labels are not known to be on agent pods.
    """
    sel = (pol.get("spec") or {}).get("podSelector")
    if not isinstance(sel, dict):
        return False
    if sel.get("matchExpressions"):
        return False
    labels = sel.get("matchLabels") or {}
    return all((k, v) == AGENT_LABEL for k, v in labels.items())


def _has_egress(pol: dict[str, Any]) -> bool:
    return "Egress" in ((pol.get("spec") or {}).get("policyTypes") or [])


def _in_agent_ns(pol: dict[str, Any], default_ns: str | None) -> bool:
    return ((pol.get("metadata") or {}).get("namespace") or default_ns) == NS


def _allows_all_egress(pol: dict[str, Any]) -> bool:
    """An egress rule with no `to` peers, or a peer of 0.0.0.0/0 or ::/0, opens every host."""
    for rule in (pol.get("spec") or {}).get("egress") or []:
        peers = (rule or {}).get("to") or []
        if not peers:
            return True
        for peer in peers:
            ip = (peer or {}).get("ipBlock") or {}
            if ip.get("cidr") in ALLOW_ALL_CIDRS:
                return True
    return False


def _agent_egress_policies(netpol: list[dict[str, Any]],
                           others: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    cands = [(d, NS) for d in netpol] + [(d, None) for docs in others.values() for d in docs]
    return [d for d, default_ns in cands
            if d.get("kind") == "NetworkPolicy" and _in_agent_ns(d, default_ns)
            and _has_egress(d) and _selects_agents(d)]


def check_egress(netpol: list[dict[str, Any]] | None,
                 others: dict[str, list[dict[str, Any]]] | None = None,
                 unparsed: dict[str, bool] | None = None) -> Row:
    if netpol is None:
        return Row("NetworkPolicy/egress", NS, "not-observed", "high", NETPOL, "spec.policyTypes",
                   "networkpolicy.yaml not readable", "Provide the file.")
    pols = _agent_egress_policies(netpol, others or {})
    hidden = sorted(p for p, names_ns in (unparsed or {}).items() if names_ns)
    rec = "Default-deny egress for agent pods with an FQDN or proxy allowlist."
    field_ = "spec.policyTypes+podSelector+metadata.namespace"
    if not pols and hidden:
        return Row("NetworkPolicy/egress", NS, "not-observed", "high", NETPOL, field_,
                   "templated files name the agent namespace and could not be parsed: " + ", ".join(hidden),
                   rec)
    if not pols:
        return Row("NetworkPolicy/egress", NS, "absent", "high", NETPOL, field_,
                   f"no Egress policy in {NS} selects {AGENT_LABEL[0]}={AGENT_LABEL[1]}", rec)
    open_ = sorted((d.get("metadata") or {}).get("name", "?") for d in pols if _allows_all_egress(d))
    if open_:
        return Row("NetworkPolicy/egress", NS, "partial", "high", NETPOL, field_,
                   "egress policy selects agent pods but is allow-all: " + ", ".join(open_), rec)
    return Row("NetworkPolicy/egress", NS, "enforced", "high", NETPOL, field_,
               "restrictive egress policy selects agent pods", rec)


_ADMIT_ORDER = {"none": 0, "agents": 1, "unknown": 2, "all": 3}
_NS_NAME_KEY = "kubernetes.io/metadata.name"


def _peer_admits(peer: dict[str, Any], policy_ns: str | None) -> str:
    """Which argo-workflows pods does one ingress peer admit: none|all|agents|unknown.

    Kubernetes rule: selectors inside ONE peer are ANDed (pods matching
    podSelector within namespaces matching namespaceSelector); peers in a
    `from` list are ORed (see `_policy_admission`). Conservative: an
    undecidable selector is `unknown`, never `agents`.
    """
    if "ipBlock" in peer:
        return "none"
    ns_sel = peer.get("namespaceSelector")
    pod_sel = peer.get("podSelector")
    if ns_sel is None:
        # Pod-only peer: resolved against the policy's own namespace.
        if policy_ns is None:
            return "unknown"
        if policy_ns != NS:
            return "none"
    elif ns_sel != {}:
        name = (ns_sel.get("matchLabels") or {}).get(_NS_NAME_KEY)
        if name is None:
            return "unknown"
        if name != NS:
            return "none"
    if not pod_sel:
        return "all"
    if (pod_sel.get("matchLabels") or {}).get(AGENT_LABEL[0]) == AGENT_LABEL[1]:
        return "agents"
    return "unknown"


def _policy_admission(pol: dict[str, Any], policy_ns: str | None) -> str:
    """Fold all ingress rules/peers (OR); broadest wins: all > unknown > agents > none."""
    result = "none"
    for rule in (pol.get("spec") or {}).get("ingress") or []:
        peers = (rule or {}).get("from") or []
        if not peers:
            return "all"
        for peer in peers:
            got = _peer_admits(peer or {}, policy_ns)
            if _ADMIT_ORDER[got] > _ADMIT_ORDER[result]:
                result = got
    return result


def check_east_west(netpol: list[dict[str, Any]] | None, others: dict[str, list[dict[str, Any]]],
                    unparsed: dict[str, bool] | None = None) -> list[Row]:
    unparsed = unparsed or {}
    if netpol is None:
        return [Row("NetworkPolicy/east-west", NS, "not-observed", "medium", NETPOL, "spec.podSelector",
                    "networkpolicy.yaml not readable", "Provide the file.")]
    wide = [d for d in netpol if d.get("kind") == "NetworkPolicy"
            and _policy_admission(d, NS) == "all"]
    rows = [Row("NetworkPolicy/east-west", NS, "partial" if wide else "enforced", "medium", NETPOL,
                "spec.ingress[].from[].podSelector",
                "ingress admits every pod in the namespace (agent -> platform pod reachable)" if wide
                else "no namespace-wide pod ingress", "Refuse agent-to-non-agent pod traffic.")]
    for path, names_ns in sorted(unparsed.items()):
        rows.append(Row("NetworkPolicy/cross-namespace", path, "not-observed", "medium", path,
                        "kind: NetworkPolicy",
                        "templated YAML, not statically parseable"
                        + ("; text names argo-workflows" if names_ns else ""),
                        "Audit the rendered manifest."))
    for path, docs in sorted(others.items()):
        for d in docs:
            adm = _policy_admission(d, (d.get("metadata") or {}).get("namespace"))
            name = (d.get("metadata") or {}).get("name", "?")
            if adm == "all":
                rows.append(Row("NetworkPolicy/cross-namespace", name,
                                "partial", "medium", path, "spec.ingress[].from[].namespaceSelector",
                                f"admits the whole {NS} namespace, agent pods included",
                                "Narrow the selector to non-agent pods."))
            elif adm == "agents":
                rows.append(Row("NetworkPolicy/cross-namespace", name,
                                "enforced", "low", path,
                                "spec.ingress[].from[].namespaceSelector+podSelector",
                                f"admits only agent pods ({AGENT_LABEL[0]}={AGENT_LABEL[1]}) from {NS}",
                                "Confirm agents should reach this destination."))
            elif adm == "unknown":
                rows.append(Row("NetworkPolicy/cross-namespace", name,
                                "not-observed", "medium", path,
                                "spec.ingress[].from[]",
                                "peer selector not statically decidable",
                                "Use matchLabels with kubernetes.io/metadata.name and an explicit podSelector."))
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
                          profiles: dict[str, list[dict[str, Any]]],
                          others: dict[str, list[dict[str, Any]]] | None = None,
                          unparsed: dict[str, bool] | None = None) -> list[Row]:
    egress = check_egress(netpol, others, unparsed)
    rows: list[Row] = []
    for path, docs in sorted(profiles.items()):
        for d in docs:
            net = ((d.get("spec") or {}).get("permissions") or {}).get("network")
            if net in (None, "none", "unrestricted"):
                continue
            name = (d.get("metadata") or {}).get("name", path)
            status = {"absent": "declared-not-enforced", "partial": "declared-not-enforced",
                      "enforced": "enforced"}.get(egress.status, "not-observed")
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
    agent_steps = 0
    for path, docs in inp.cwfts.items():
        for cwft, tpl, is_agent, spec in classify_templates(docs):
            if not is_agent:
                continue
            agent_steps += 1
            rows += check_identity(path, cwft, tpl, spec, bound)
            rows += check_pod_security(path, cwft, tpl, spec)
            rows.append(check_runtime_class(path, cwft, tpl, spec))
            rows += check_resources(path, cwft, tpl, spec)
            rows.append(check_credentials_env(path, cwft, tpl))
    if inp.cwfts and not agent_steps:
        rows.append(Row("PodSecurity", "mctl-agents CWFTs", "not-observed", "high", CWFT_GLOB, "templates",
                        f"{len(inp.cwfts)} CWFT file(s) read but no template runs image {AGENT_IMAGE}",
                        "Check the agent image parameter name."))
    rows.append(check_egress(inp.netpol, inp.other_netpols, inp.unparsed_netpols))
    rows += check_east_west(inp.netpol, inp.other_netpols, inp.unparsed_netpols)
    rows.append(check_quota(inp.quota))
    rows += check_profile_network(inp.netpol, inp.profiles, inp.other_netpols, inp.unparsed_netpols)
    return rows


def render(rows: list[Row], inp: Inputs, fmt: str) -> str:
    if fmt == "json":
        classified = {p: [{"cwft": c, "template": t.get("name"), "agent": a}
                          for c, t, a, _ in classify_templates(d)] for p, d in inp.cwfts.items()}
        return json.dumps({"schema_version": SCHEMA_VERSION, "files_read": sorted(inp.cwfts), "templates": classified,
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
