#!/usr/bin/env python3
"""Operator CLI for the context-strategy release catalog
(mctlhq/mctl-agents#472, ADR 019). Mirrors `tools/publish_agent_release.py`'s
shape: argparse, `--dry-run`, writes files, prints what it did. Every
subcommand delegates its actual validation to `orchestrator/context_release.py`
— this file is I/O only.

Because the catalog is committed in this repository, every one of these is a
reviewed commit: the promoter identity is the commit author and the audit
trail is git history.

    python tools/context_release.py publish --strategy deterministic-fixed-order --version 1.0.0
    python tools/context_release.py promote --agent issue-investigator --environment shadow \\
        --strategy deterministic-fixed-order --version 1.0.0 --promoted-by octocat \\
        --reason "inert shadow baseline"
    python tools/context_release.py rollback --agent issue-investigator --environment shadow \\
        --to-revision 1 --promoted-by octocat --reason "revert bad promotion"
    python tools/context_release.py resolve --agent issue-investigator --environment shadow
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator import context_release as cr  # noqa: E402 — after sys.path setup, matches tools/capability_bench.py

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _catalog_name(value: str) -> str:
    """argparse `type=` for `--agent`/`--environment`: reject anything that
    is not a single safe path segment, so `BINDINGS_DIR / environment /
    f"{agent}.yaml"` can never escape the catalog directory via `..`, an
    absolute path, an embedded `/`, or a null byte."""
    if not _SAFE_NAME_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"must be a single path segment matching {_SAFE_NAME_RE.pattern!r}, got {value!r}"
        )
    return value


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_yaml(path: Path, document: dict, *, dry_run: bool) -> None:
    if dry_run:
        print(f"  would write {path.relative_to(REPO_ROOT)}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(document, f, sort_keys=False, default_flow_style=False)
    print(f"  wrote {path.relative_to(REPO_ROOT)}")


def cmd_publish(args: argparse.Namespace) -> bool:
    try:
        document = cr.build_version_document(args.strategy, args.version, lifecycle=args.lifecycle)
    except cr.ContextReleaseError as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        return False
    path = cr.VERSIONS_DIR / args.strategy / f"{args.version}.yaml"
    implementation_hash = document["spec"]["implementation"]["implementationHash"]
    print(f"  {args.strategy}@{args.version} implementationHash={implementation_hash}")
    _write_yaml(path, document, dry_run=args.dry_run)
    return True


def cmd_promote(args: argparse.Namespace) -> bool:
    try:
        binding = cr.load_binding_or_none(args.agent, args.environment)
        updated = cr.promote(
            binding,
            agent=args.agent,
            environment=args.environment,
            strategy_name=args.strategy,
            strategy_version=args.version,
            promoted_by=args.promoted_by,
            reason=args.reason,
            promoted_at=_now_iso(),
            evidence_kind=args.evidence_kind,
            evidence_ref=args.evidence_ref,
            evidence_evaluator_version=args.evidence_evaluator_version,
        )
    except cr.ContextReleaseError as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        return False
    path = cr.BINDINGS_DIR / args.environment / f"{args.agent}.yaml"
    print(f"  {args.agent}/{args.environment} -> revision {updated.active.revision}: {args.strategy}@{args.version}")
    _write_yaml(path, updated.to_dict(), dry_run=args.dry_run)
    return True


def cmd_rollback(args: argparse.Namespace) -> bool:
    try:
        binding = cr.load_binding(args.agent, args.environment)
        updated = cr.rollback(
            binding,
            to_revision=args.to_revision,
            promoted_by=args.promoted_by,
            reason=args.reason,
            promoted_at=_now_iso(),
        )
    except cr.ContextReleaseError as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        return False
    path = cr.BINDINGS_DIR / args.environment / f"{args.agent}.yaml"
    target = next(r for r in binding.history if r.revision == args.to_revision)
    print(
        f"  {args.agent}/{args.environment} -> revision {updated.active.revision} "
        f"(rollbackOf={args.to_revision}): {target.strategy}@{target.version}"
    )
    _write_yaml(path, updated.to_dict(), dry_run=args.dry_run)
    return True


def cmd_resolve(args: argparse.Namespace) -> bool:
    try:
        resolved = cr.resolve(args.agent, args.environment)
    except cr.ContextReleaseError as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        return False
    print(
        f"  {args.agent}/{args.environment} resolves revision {resolved.release_revision}: "
        f"{resolved.strategy}@{resolved.version} (content_hash={resolved.content_hash}, "
        f"implementation_hash={resolved.implementation_hash}, verdict={resolved.verdict})"
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    publish = sub.add_parser("publish", help="write/refresh a ContextStrategyVersion document")
    publish.add_argument("--strategy", required=True, help="strategy name (must be one context_assembly.py implements)")
    publish.add_argument("--version", required=True, help="semver X.Y.Z")
    publish.add_argument("--lifecycle", default=None, choices=sorted(cr.LIFECYCLE_VALUES))
    publish.add_argument("--dry-run", action="store_true")
    publish.set_defaults(func=cmd_publish)

    promote = sub.add_parser("promote", help="append a promotion revision to a binding")
    promote.add_argument("--agent", required=True, type=_catalog_name)
    promote.add_argument("--environment", required=True, type=_catalog_name)
    promote.add_argument("--strategy", required=True)
    promote.add_argument("--version", required=True)
    promote.add_argument("--promoted-by", required=True)
    promote.add_argument("--reason", required=True)
    promote.add_argument("--evidence-kind", default="none", choices=sorted(cr.EVIDENCE_KINDS))
    promote.add_argument("--evidence-ref", default=None)
    promote.add_argument("--evidence-evaluator-version", default=None)
    promote.add_argument("--dry-run", action="store_true")
    promote.set_defaults(func=cmd_promote)

    rollback = sub.add_parser("rollback", help="append a revision restoring an exact prior one")
    rollback.add_argument("--agent", required=True, type=_catalog_name)
    rollback.add_argument("--environment", required=True, type=_catalog_name)
    rollback.add_argument("--to-revision", required=True, type=int)
    rollback.add_argument("--promoted-by", required=True)
    rollback.add_argument("--reason", required=True)
    rollback.add_argument("--dry-run", action="store_true")
    rollback.set_defaults(func=cmd_rollback)

    resolve = sub.add_parser("resolve", help="print what a run would resolve today (also the CI preflight)")
    resolve.add_argument("--agent", required=True, type=_catalog_name)
    resolve.add_argument("--environment", required=True, type=_catalog_name)
    resolve.set_defaults(func=cmd_resolve)

    args = parser.parse_args()
    try:
        ok = args.func(args)
    except Exception as exc:  # noqa: BLE001 — one bad target must not crash the CLI without a message
        print(f"  ERROR: {exc!r}", file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
