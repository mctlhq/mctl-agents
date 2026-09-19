"""What an implementer run actually did, read from Argo's node graph (#395).

The implement CWFT is a step graph — `implement` (run-implementer),
`implement-fallback` (run-implementer, on a second OAuth account),
`commit` (commit-and-push), `assert-produced` (assert-attempt) — and its
workflow-level phase collapses that graph to one word. `Failed` alone
cannot say WHETHER the implementer ever ran, and that distinction is the
one that decides what the orchestrator does next:

    pre_start     no implementer pod ever ran: it waited on capacity or a
                  mutex and the workflow deadline killed it, or Argo
                  accepted the workflow and never scheduled the pod.
                  Nothing was attempted → requeue, no human, no attempt
                  counted.
    execution     the implementer pod ran and did not succeed.
    finalization  the implementer succeeded, but commit-and-push or the
                  produced-attempt assertion failed: the work exists and
                  its record may not.
    success       the workflow succeeded.

On 2026-09-19 five of six lost proposals were `pre_start` — "Step exceeded
its deadline" on a node that never had a pod — and one was `execution`
("Pod was active on the node longer than the specified deadline"). All six
were recorded as the same Failed, and all six needed a human.

Pure functions over the status block `submit_and_wait` already polls, so
the activity can observe and the workflow can decide without either one
importing the other's internals.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

IMPLEMENTER_TEMPLATE = "run-implementer"
FINALIZATION_TEMPLATES = frozenset({"commit-and-push", "assert-attempt"})

FailureClass = Literal["pre_start", "execution", "finalization"]
# Derived, not spelled out a second time: two independent literals drift,
# and the one that would drift silently is the one the error types are
# built from.
Outcome = Literal["success"] | FailureClass


@dataclass(frozen=True)
class ImplementerObservation:
    """The implementer's own story, independent of the workflow phase."""

    # Did any run-implementer pod actually execute? None = the node graph
    # was not readable (no `nodes` in the status block), which must not be
    # mistaken for "no".
    ran: bool | None
    # Best phase across the implement and implement-fallback nodes: a
    # Succeeded fallback after a Failed primary is Succeeded.
    phase: str | None
    # When the first implementer pod started, from Argo. This is the
    # `started_at` of the implementation attempt — not the workflow's
    # startedAt, which is set on submission while the node may still be
    # Pending on a mutex.
    started_at: str | None
    # Phase of the finalization steps, worst-of.
    finalization_phase: str | None


def _pod_ran(node: dict[str, Any]) -> bool:
    """A Pod node that reached a kubelet, by any of the marks Argo leaves.

    `startedAt` is NOT one of them: Argo stamps it when the node is created,
    which for a node blocked on a synchronization lock is while it is
    Pending with no pod at all — exactly the case this must say "no" to.
    """
    if node.get("hostNodeName"):
        return True
    outputs = node.get("outputs") or {}
    if isinstance(outputs, dict) and outputs.get("exitCode") is not None:
        return True
    return node.get("phase") == "Succeeded"


def _template_of(node: dict[str, Any]) -> str:
    """The template a node ran, under either of the two spellings.

    A step resolved through `templateRef` carries `templateRef: {name,
    template}` and leaves `templateName` empty on the node, so reading only
    `templateName` would stop recognising the implementer the day the CWFT
    pulls it in by reference rather than inline.
    """
    name = node.get("templateName")
    if name:
        return str(name)
    ref = node.get("templateRef") or {}
    if isinstance(ref, dict) and ref.get("template"):
        return str(ref["template"])
    return ""


_PHASE_RANK = {"Succeeded": 3, "Running": 2, "Pending": 1, "Failed": 0, "Error": 0}
# A step Argo never scheduled (a fallback whose `when` was false, an
# assertion after a failed commit) says nothing about the run and is left
# out of both the best-of and the worst-of below.
_NOT_A_VERDICT = frozenset({"Omitted", "Skipped"})


def observe_implementer(status_block: dict[str, Any]) -> ImplementerObservation:
    nodes = status_block.get("nodes")
    # An EMPTY node map is unreadable, not empty-because-nothing-ran. Argo
    # offloads `status.nodes` to its own store once the graph outgrows the
    # etcd limit and prunes it on archival, and it is also simply not
    # populated yet on a freshly accepted workflow — in all three cases the
    # map is `{}` while a pod may well have run. Reporting that as "did not
    # run" would requeue an implementer that already produced a commit, so
    # it is reported as unknown, which `classify` turns into an execution
    # failure a human looks at rather than a silent second attempt.
    if not isinstance(nodes, dict) or not nodes:
        return ImplementerObservation(ran=None, phase=None, started_at=None, finalization_phase=None)

    pods = [
        n for n in nodes.values()
        if isinstance(n, dict) and n.get("type") == "Pod" and n.get("phase") not in _NOT_A_VERDICT
    ]
    implementer = [n for n in pods if _template_of(n) == IMPLEMENTER_TEMPLATE]
    finalization = [n for n in pods if _template_of(n) in FINALIZATION_TEMPLATES]

    # A graph with pods in it but none of them the implementer is not proof
    # that the implementer did not run — it is proof that this module and
    # the CWFT no longer agree on its name. The template lives in another
    # repository (cwft-mctl-agents-implement.yaml in mctl-gitops) and no CI
    # here can see a rename, so the mismatch has to fail closed: reported
    # unknown, which classifies as an execution failure a human reads,
    # instead of `ran=False`, which would requeue an implementer that may
    # have committed and pushed.
    if pods and not implementer:
        return ImplementerObservation(ran=None, phase=None, started_at=None, finalization_phase=None)

    ran_nodes = [n for n in implementer if _pod_ran(n)]
    phase: str | None = None
    if implementer:
        phase = max((str(n.get("phase") or "Pending") for n in implementer), key=lambda p: _PHASE_RANK.get(p, 1))
    started = sorted(str(n["startedAt"]) for n in ran_nodes if n.get("startedAt"))

    fin_phase: str | None = None
    if finalization:
        fin_phase = min(
            (str(n.get("phase") or "Pending") for n in finalization), key=lambda p: _PHASE_RANK.get(p, 1)
        )

    return ImplementerObservation(
        ran=bool(ran_nodes),
        phase=phase,
        started_at=started[0] if started else None,
        finalization_phase=fin_phase,
    )


def classify(
    workflow_phase: str,
    *,
    implementer_ran: bool | None,
    implementer_phase: str | None,
    finalization_phase: str | None = None,
) -> Outcome:
    """The decision `DevLoopWorkflow` makes after an implement submit.

    Unknown is treated as `execution`, not `pre_start`. A requeue resubmits
    the implementer, and resubmitting one that may have run is the
    duplicate-attempt failure the whole resume design exists to prevent;
    a human looking at a Failed loop is the cheaper mistake.

    `finalization` is deliberately "the workflow failed AFTER the
    implementer succeeded", not "a commit-and-push node reported Failed".
    It therefore also covers a failing exit handler, a workflow-level
    deadline that fires past the last step, and a podGC error — none of
    which touched the implementation. That is the distinction the recovery
    plane (#353, mctl-api#294) needs from this name: the code was written,
    so recovery is about the wrap-up and never about re-running the
    implementer.

    `finalization_phase` sharpens the message through
    `finalization_evidence` when Argo left a node to read, and deliberately
    does NOT move the verdict to `execution` when it is clean or missing.
    The two defaults fail in opposite directions and are not equally
    priced: calling it `finalization` when nothing visibly failed sends
    recovery looking for a commit that already exists, which is a read;
    calling it `execution` when the implementer in fact succeeded invites
    recovery to run the implementer again, which is the duplicate attempt
    this design exists to prevent. A missing node graph — routine once Argo
    offloads or prunes `status.nodes` — would take that second branch every
    time.
    """
    if workflow_phase == "Succeeded":
        return "success"
    if implementer_ran is False:
        return "pre_start"
    if implementer_ran and implementer_phase == "Succeeded":
        return "finalization"
    return "execution"


def finalization_evidence(finalization_phase: str | None) -> str:
    """How a `finalization` verdict was reached, for the human and #353.

    The verdict says the implementation happened and the wrap-up did not
    finish. This says whether Argo showed a failed finalization node or
    whether the failure is past the last step it left behind — an exit
    handler, a workflow deadline, a terminate — so recovery knows whether
    to expect a missing commit or a complete one.
    """
    if finalization_phase in {"Failed", "Error"}:
        return f"the commit/assert step reported {finalization_phase}"
    if finalization_phase is None:
        return "no finalization node was readable, so the failure is somewhere after the implementer"
    if finalization_phase in {"Pending", "Running"}:
        return (
            f"the commit/assert step was still {finalization_phase} at the last readable poll, so "
            "the wrap-up was cut short rather than completed and the commit may not exist"
        )
    return (
        f"the finalization steps reported {finalization_phase}, so the failure is past them "
        "(exit handler, workflow deadline or terminate) and the commit may well exist"
    )
