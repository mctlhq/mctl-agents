"""mctl platform configuration."""
from pathlib import Path

from config.model_policy import resolve_model

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"

# Platform services. Append as the platform grows.
#
# This list gates two things:
#   1) Service-agent rotation (researcher / analyst / spec-writer cron).
#   2) Tier 2 implementer's `--service` filter — only proposals under
#      agents-state/<svc>/ where svc is in SERVICES are eligible.
#
# `mctl-agents` (the agent platform itself) is included so the implementer
# can accept self-improvement proposals targeting this repo (new
# orchestrator modules, sub-agent prompts, etc.). The service-agent
# rotation skips it — analysis of the agent platform is the mentor's job
# via _mentor/ aggregations, not a per-service researcher run.
#
# `mctl-telegram` is included as a valid implementer/investigator target
# but is NOT a rotation service: it has no agents/<svc>/ scaffold and is
# driven only by GitHub-issue proposals via the issue-investigator.
#
# `mctl-design` is the autonomous-pipeline pilot (design system + Storybook
# at ui.mctl.ai). It is an issue-driven implementer target only — no
# proactive rotation, and its review/fix/merge is owned by pr-steward, not
# the shepherd (see SHEPHERD_SKIP_SERVICES in run_shepherd.py). The generic
# implementer sub-agent (agents/_generic/.claude/agents/implementer.md)
# covers it; no per-service scaffold.
#
# `mctl-academy` is a learning product whose content is generated from
# external documentation. Its intake is a weekly source-drift job in its own
# repo that opens an `agents:intake` issue when an upstream doc's hash
# changes — so registration here is what lets run_issue_poller.py dispatch
# that issue at all (it skips issues whose repo is not in SERVICES). It is
# NOT a rotation service: the rotation prompt researches dependency releases
# and CVEs, which cannot detect external-source drift, and it would need an
# agents/mctl-academy/ scaffold that deliberately does not exist. Its PRs are
# also excluded from the shepherd (SHEPHERD_SKIP_SERVICES in the gitops
# CronWorkflow) and are not steward-owned — content merges are gated on a
# human CODEOWNER by design.
#
# `portfolio` is a static Astro site in tenant `labs`, developed exclusively
# through the DevLoop. It has no agents/portfolio/ scaffold, so it must not
# enter ROTATING_SERVICES — the rotation would fail immediately without a
# scaffold. Its intake is human-labelled `agents:intake` issues, dispatched
# by run_issue_poller.py.
#
# `.github` is the org-profile repo, and since #69 it also holds the roadmap
# control plane (`roadmap/`, EpicDefinition) plus the org-wide reusable
# workflows every other repo calls. It is DevLoop-driven the same way
# `portfolio` is: human-labelled `agents:intake` issues, no
# `agents/.github/` scaffold, so it must not enter ROTATING_SERVICES. Two
# things about this repo that the others do not have: a change under
# `.github/workflows/` is org-wide CI, and the agents-state directory for it
# is a dotted path (`agents-state/.github/`) — every consumer reaches it
# through `Path.iterdir()` or a git `:(glob)` pathspec, both of which match a
# leading dot, but a shell glob would not.
#
# `newton-mcp-gateway` is a private Python/uv MCP server in tenant `labs`,
# developed exclusively through the DevLoop (epic
# `mctlhq/newton-mcp-gateway#1`). Registered here so `run_issue_poller.py`
# will dispatch its `agents:intake` issues at all — it skips any issue whose
# repo is not in SERVICES. It has no `agents/newton-mcp-gateway/` scaffold,
# so it must NOT enter ROTATING_SERVICES: the proactive
# researcher/analyst/spec-writer rotation reads
# `AGENTS_DIR / <service> / CLAUDE.md` and would fail immediately. The
# generic implementer sub-agent
# (agents/_generic/.claude/agents/implementer.md) covers it. Its PRs stay
# shepherd-owned — no SHEPHERD_SKIP_SERVICES entry.
#
# `mctl-claude-remote` is issue-driven only (human-labelled `agents:intake`,
# first target `mctlhq/mctl-claude-remote#79`). Registered so
# `run_issue_poller.py` dispatches it. Like `newton-mcp-gateway` it has no
# `agents/mctl-claude-remote/` scaffold, so it must NOT enter
# ROTATING_SERVICES; the generic implementer sub-agent covers it and its PRs
# stay shepherd-owned (no SHEPHERD_SKIP_SERVICES entry).
#
# `mctl-alice` (public Yandex smart-home MCP server, deployed in tenant
# `labs`) is issue-driven only, registered for its public-launch hardening
# issues. Same shape as `mctl-claude-remote`: no `agents/mctl-alice/`
# scaffold, so it must NOT enter ROTATING_SERVICES; the generic implementer
# covers it and its PRs stay shepherd-owned.
SERVICES = [
    "mctl-web",
    "mctl-docs",
    "mctl-api",
    "mctl-portal",
    "mctl-agent",
    "mctl-gitops",
    "mctl-agents",
    "mctl-telegram",
    "mctl-design",
    "mctl-pairdesk",
    "mctl-academy",
    "seerrsense",
    "portfolio",
    ".github",
    "newton-mcp-gateway",
    "mctl-claude-remote",
    "mctl-alice",
    # "upwork-mcp",
]

# The mctl-api tenant that owns each service's DevLoop WorkItems
# (mctlhq/mctl-agents#636). `POST /api/v1/work-items` takes an explicit
# tenant, and an unmapped service is a hard start failure, never a default:
# the issue's work item must land in the tenant where every other surface
# opens it. mctl-api dedupes open work per (tenant, external_key), and the
# Telegram surface opens an issue's item in `mctl` (MCTL_WORK_ITEM_TENANT in
# mctl-gitops services/labs/mctl-telegram/values.yaml) whatever namespace the
# target deploys to, so a DevLoop item in another tenant would split one
# issue into two WorkItems. A new SERVICES entry must be added
# here (tests/test_settings.py::test_every_service_has_a_work_item_tenant fails
# otherwise).
PLATFORM_TENANT = "mctl"
SERVICE_TENANTS = {
    "mctl-web": PLATFORM_TENANT,
    "mctl-docs": PLATFORM_TENANT,
    "mctl-api": PLATFORM_TENANT,
    "mctl-portal": PLATFORM_TENANT,
    "mctl-agent": PLATFORM_TENANT,
    "mctl-gitops": PLATFORM_TENANT,
    "mctl-agents": PLATFORM_TENANT,
    "mctl-telegram": PLATFORM_TENANT,
    "mctl-design": PLATFORM_TENANT,
    "mctl-pairdesk": PLATFORM_TENANT,
    "mctl-academy": PLATFORM_TENANT,
    "seerrsense": PLATFORM_TENANT,
    "portfolio": PLATFORM_TENANT,
    ".github": PLATFORM_TENANT,
    "newton-mcp-gateway": PLATFORM_TENANT,
    "mctl-claude-remote": PLATFORM_TENANT,
    "mctl-alice": PLATFORM_TENANT,
}


def tenant_for_service(service: str) -> str | None:
    """The tenant that owns `service`'s WorkItems, or None when unmapped."""
    return SERVICE_TENANTS.get(service)


# Services that are valid implementer/investigator targets but are NOT
# analyzed by the proactive researcher/analyst/spec-writer rotation.
NON_ROTATING_SERVICES = {
    "mctl-agents",
    "mctl-telegram",
    "mctl-design",
    "mctl-pairdesk",
    "mctl-academy",
    "seerrsense",
    "portfolio",
    ".github",
    "newton-mcp-gateway",
    "mctl-claude-remote",
    "mctl-alice",
}

# Subset of SERVICES that the proactive R&D rotation analyzes via
# researcher/analyst/spec-writer. Anything in SERVICES but NOT here is
# accepted by the implementer (--service filter) but is not subject to
# automatic proposal generation; proposals for those services are
# authored by the mentor, by humans, or by the issue-investigator.
ROTATING_SERVICES = [s for s in SERVICES if s not in NON_ROTATING_SERVICES]

MENTOR_DIR = AGENTS_DIR / "_mentor"
SHEPHERD_DIR = AGENTS_DIR / "_shepherd"

# mctl MCP — shared by every agent
MCTL_MCP_URL = "https://api.mctl.ai/mcp"

# Models. Task-specific env vars remain supported as highest-priority
# overrides so existing workflow configuration remains backwards compatible.
SERVICE_AGENT_SELECTION = resolve_model(
    "service_agent",
    legacy_model_env="SERVICE_AGENT_MODEL",
)
MENTOR_SELECTION = resolve_model(
    "mentor_digest",
    legacy_model_env="MENTOR_MODEL",
)
SHEPHERD_SELECTION = resolve_model(
    "review_findings_normalize",
    legacy_model_env="SHEPHERD_MODEL",
)
SCOUT_RELEVANCE_SELECTION = resolve_model("scout_relevance")
SCOUT_SCORING_SELECTION = resolve_model("scout_scoring")

SERVICE_AGENT_MODEL = SERVICE_AGENT_SELECTION.model
MENTOR_MODEL = MENTOR_SELECTION.model
SHEPHERD_MODEL = SHEPHERD_SELECTION.model


# Per-target CI type-check commands the Tier 2 implementer runs before its
# first push (mctl-agents#785). Explicit, not auto-detected: a service is
# listed only once its CI is confirmed to run the same command. The mctl-agents
# entry mirrors the "Run mypy" step in .github/workflows/pr-validation.yml.
TYPECHECK_COMMANDS: dict[str, tuple[str, ...]] = {
    "mctl-agents": ("uv", "run", "--locked", "mypy"),
}

# Hard cap for one pre-push type-check step (install or mypy), in seconds.
IMPLEMENTER_TYPECHECK_TIMEOUT_SECONDS = 300
