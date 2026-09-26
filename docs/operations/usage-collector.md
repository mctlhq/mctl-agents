# Usage-collector — owner steps (mctlhq/mctl-agents#506)

`orchestrator/run_usage_collector.py` ships in this repo, on the existing
`ghcr.io/mctlhq/mctl-agents` image, runnable today as a one-shot:

```bash
python -m orchestrator.run_usage_collector --dry-run
```

**Nothing in this document is applied by this change.** The
implementer works in `mctl-agents` and cannot commit to `mctl-gitops`; the
manifests below are the owner's checklist to wire the collector to a
schedule. Until an owner applies (a) and (b), the collector only runs when
someone invokes it by hand.

**Expect zero artifacts almost everywhere, at first.** As of 2026-09-26,
only `mctlhq/.github` calls the reusable reviewer workflow locally
(`uses: ./.github/workflows/claude-review.yml`); every other caller
repository pins a revision of `mctlhq/.github/.github/workflows/claude-review.yml`
that predates the usage-capture change (mctlhq/.github#126), so those
repositories publish no `model-usage-records` artifact yet. A collector run
that reports `artifacts_found=0` for every repository except `.github` is
the expected state, not a broken collector — see step (d) below for the
per-repository fix.

## (a) ClusterWorkflowTemplate

Apply to `platform-gitops/argo-workflows/cluster-templates/cwft-mctl-agents-usage-collector.yaml`.
Modelled on `cwft-mctl-agents-reconcile.yaml` for the container/env shape,
but simpler: this workflow reads artifacts and writes to mctl-api only — it
never clones `mctl-gitops`, never writes to git, and takes no
`mctl-gitops-main-writes` mutex. `emptyDir` workdir only, no PVC.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ClusterWorkflowTemplate
metadata:
  name: mctl-agents-usage-collector
  annotations:
    workflows.argoproj.io/description: |
      Pulls reviewer-stage model-usage-records artifacts (claude-review.yml,
      mctlhq/.github#126) into the mctl-api usage ledger (ADR-012,
      mctlhq/mctl-agents#506).

      Runs `python -m orchestrator.run_usage_collector` inside
      ghcr.io/mctlhq/mctl-agents. Read-only against GitHub (gh api GET
      only); the only write is a POST to mctl-api's confined usage-writer
      endpoint. No gitops clone, no git push, no mutex.
spec:
  serviceAccountName: argo-workflow-sa
  imagePullSecrets:
    - name: ghcr-credentials
  podMetadata:
    labels:
      app.kubernetes.io/name: mctl-agents
      mctl.ai/team: admins
  ttlStrategy:
    secondsAfterCompletion: 259200
  # A bounded HTTP sweep: SERVICES.length list calls plus a handful of
  # artifact downloads. Generous margin over the realistic ceiling, not a
  # measured floor.
  activeDeadlineSeconds: 900
  entrypoint: collect-usage
  onExit: notify-telegram

  arguments:
    parameters:
      - name: lookback_days
        value: "3"
        # The scheduled tick's default. Pass "90" for the one-shot backfill
        # after un-suspending (step c below) — the ledger dedupes, so a
        # widened window is a safe redundant read, never a double count.
      - name: dry_run
        value: "false"
      - name: agent_image
        value: ghcr.io/mctlhq/mctl-agents:1.58.0
        # Bump alongside the other CWFTs' agent_image default; the registry's
        # resolved image_ref overrides this once a caller pins per-run.

  volumes:
    - name: workdir
      emptyDir:
        sizeLimit: 256Mi

  templates:
    - name: collect-usage
      container:
        image: "{{workflow.parameters.agent_image}}"
        imagePullPolicy: IfNotPresent
        command: ["/entrypoint.sh"]
        args:
          - "sh"
          - "-c"
          - |
            set -e
            set -- python -m orchestrator.run_usage_collector \
              --lookback-days "$WORKFLOW_LOOKBACK_DAYS"
            if [ "$WORKFLOW_DRY_RUN" = "true" ]; then
              set -- "$@" --dry-run
            fi
            printf '→'; printf ' %s' "$@"; printf '\n'
            exec "$@"
        env:
          - name: WORKFLOW_LOOKBACK_DAYS
            value: "{{workflow.parameters.lookback_days}}"
          - name: WORKFLOW_DRY_RUN
            value: "{{workflow.parameters.dry_run}}"
          # Read-only against GitHub Actions artifacts. NOT optional: a
          # missing secret should fail at pod-scheduling time, not as an
          # opaque `gh` auth error mid-sweep.
          - name: GITHUB_TOKEN
            valueFrom:
              secretKeyRef:
                name: mctl-agents-secrets
                key: github-token
          # optional: true — matches every other CWFT wiring this secret
          # (e.g. cwft-mctl-agents-implement.yaml). An unset writer token
          # makes the collector log one warning, post nothing, and exit
          # zero (see run_usage_collector.py's module docstring).
          - name: MCTL_USAGE_WRITER_TOKEN
            valueFrom:
              secretKeyRef:
                name: mctl-agents-secrets
                key: usage-writer-token
                optional: true
          - name: MCTL_API_BASE_URL
            value: https://api.mctl.ai
        resources:
          requests: { cpu: 100m, memory: 128Mi, ephemeral-storage: 256Mi }
          limits:   { cpu: 500m, memory: 512Mi, ephemeral-storage: 512Mi }
        volumeMounts:
          - name: workdir
            mountPath: /workdir

    # ── Telegram notification (onExit) ──────────────────────────────────
    # Same "quiet unless it did something worth flagging" policy as the
    # reconcile CWFT: a routine Succeeded tick reports nothing, since
    # near-zero artifacts is the expected state until step (d) lands.
    - name: notify-telegram
      script:
        image: alpine:3.19
        command: [sh]
        env:
          - name: TG_BOT_TOKEN
            valueFrom:
              secretKeyRef:
                name: mctl-agents-secrets
                key: telegram-bot-token
                optional: true
          - name: TG_CHAT_ID
            valueFrom:
              secretKeyRef:
                name: mctl-agents-secrets
                key: telegram-chat-id
                optional: true
          - name: WORKFLOW_STATUS
            value: "{{workflow.status}}"
          - name: WORKFLOW_NAME
            value: "{{workflow.name}}"
          - name: WORKFLOW_DURATION
            value: "{{workflow.duration}}"
        source: |
          set +e
          if [ "$WORKFLOW_STATUS" = "Succeeded" ]; then
            echo "usage-collector succeeded; routine tick, skipping Telegram notify."
            exit 0
          fi
          apk add --no-cache curl ca-certificates >/dev/null 2>&1
          case "$WORKFLOW_STATUS" in
            Failed) ICON="❌" ;;
            Error)  ICON="⚠️" ;;
            *)      ICON="⏳" ;;
          esac
          UI_URL="https://workflows.mctl.ai/workflows/argo-workflows/${WORKFLOW_NAME}"
          TEXT="${ICON} mctl-agents: usage-collector — ${WORKFLOW_STATUS} (${WORKFLOW_DURATION}s)\n${UI_URL}"
          if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
            curl -s -o /dev/null -w 'telegram http=%{http_code}\n' \
              -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
              -H "Content-Type: application/json" \
              -d "{\"chat_id\":\"${TG_CHAT_ID}\",\"text\":\"${TEXT}\",\"disable_web_page_preview\":true}"
          else
            echo "ℹ️  TG_BOT_TOKEN or TG_CHAT_ID not set; skipping Telegram leg."
          fi
          exit 0
```

## (b) CronWorkflow wrapper

Apply to `platform-gitops/argo-workflows/cluster-templates/cronworkflow-mctl-agents-usage-collector.yaml`.
Modelled on `cronworkflow-mctl-agents-incidents.yaml`.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: CronWorkflow
metadata:
  name: mctl-agents-usage-collector
  namespace: argo-workflows
  annotations:
    workflows.argoproj.io/description: |
      Hourly trigger for the reviewer-usage collector
      (mctlhq/mctl-agents#506). Workflow logic lives in ClusterWorkflowTemplate
      mctl-agents-usage-collector. Suspended on first apply — see
      docs/operations/usage-collector.md in mctl-agents for the
      un-suspend + backfill checklist.
spec:
  # :21 — the family already owns :00 (token rotation), :03 (reconcile),
  # :07 (issue-poll), :09 (shepherd), :11/:15 (incidents). The collector
  # takes no gitops mutex and pushes nothing, so the offset is only about
  # not bunching GitHub API calls with the rest of the platform in the same
  # minute; it is not contending for a lock any of those hold.
  schedule: "21 * * * *"
  timezone: "UTC"
  concurrencyPolicy: Forbid
  startingDeadlineSeconds: 300
  successfulJobsHistoryLimit: 168
  failedJobsHistoryLimit: 48
  # Un-suspend only after: the Vault secret `usage-writer-token` exists at
  # mctl-agents-secrets (optional: true above means the collector degrades
  # gracefully without it, but a suspended-forever cron collects nothing),
  # and a manual `--dry-run` one-shot has been sanity-checked against
  # mctlhq/.github, the one repository already producing artifacts.
  suspend: true
  workflowSpec:
    workflowTemplateRef:
      name: mctl-agents-usage-collector
      clusterScope: true
```

## (c) One-shot backfill after un-suspending

Artifact retention is 90 days (`retention-days: 90` in the reusable
workflow). Once the CronWorkflow is un-suspended, run the backfill once so
the scheduled `--lookback-days 3` tick isn't the only pass over the
existing 90-day backlog:

```bash
argo submit --from clusterworkflowtemplate/mctl-agents-usage-collector \
  -p lookback_days=90 \
  --generate-name mctl-agents-usage-collector-backfill- \
  -n argo-workflows
```

Safe to re-run: the collector is stateless and the ledger dedupes on
`(session_id, result_uuid, model_key)`, so a repeated backfill adds no row.

## (d) Per-repository `uses:` SHA bump

Each caller repository's own `.github/workflows/claude-review.yml` pins the
reusable workflow at a specific commit SHA. As of 2026-09-26, every caller
except `mctlhq/.github` itself pins a revision that predates the
usage-capture change (`b0fc8003…`, or `f25ff6a4…` for `mctl-telegram`), so
those repositories currently publish no `model-usage-records` artifact —
the collector will faithfully report `artifacts_found=0` for them until
this step lands. This is a per-repository change (bump the pinned `uses:`
SHA to a revision at or after mctlhq/.github#126), tracked here as an owner
step rather than performed by this proposal, which explicitly does not
touch any caller repository's workflow file.

## (e) Optional: a narrowed rotation target

`mctl-agents-secrets/github-token` — the credential the CWFT above reuses —
is minted **unscoped** from the shared `mctl-agents` App installation
(`cwft-rotate-github-token.yaml`), which carries `contents:write`,
`actions:write`, `issues:write` and `pull_requests:write` across every
repository it covers. The collector only ever needs `actions:read` and
`metadata:read`. Reusing the existing secret satisfies the issue's actual
constraint ("no new secret in GitHub" — this is a new *consumer* of an
already-existing one, not a new secret), so it is not required, but a
narrowed target is a small, self-contained addition to
`cwft-rotate-github-token.yaml`'s `TARGETS` list — the same shape
`mctl-api`'s narrowed target already uses:

```python
{
    "label": "usage-collector",
    "creds_path": "platform/github-app-agents",
    "dest_path": "platform/mctl-agents/usage-collector",
    "dest_key": "github-token",
    "external_secrets": [
        ("argo-workflows", "mctl-agents-secrets"),
    ],
    "scope": {
        "permissions": {"actions": "read", "metadata": "read"},
    },
},
```

then point the CWFT's `GITHUB_TOKEN` at a new
`usage-collector-github-token` secret key instead of the shared
`github-token`. Left undone, the collector still cannot mutate anything —
every call it makes is `gh api` against a `GET` endpoint — so this step
narrows the credential's blast radius, not the collector's own behaviour.
