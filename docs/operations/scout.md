# Scout: daily tech-news digest (mctlhq/mctl-agents#797)

`orchestrator/run_scout.py` fetches the RSS/Atom feeds in
`agents/_scout/sources.yaml`, keeps items from the last 26 hours, drops those
already filed as `spike:scout` issues, filters them with a cheap relevance pass,
scores the rest against `agents/_scout/PLATFORM.md` and the spike backlog, and
sends one Telegram message with the top 10. Each item has a "Draft spike" URL
button that opens a pre-filled GitHub new-issue form. The scout never writes to
GitHub; it only calls Telegram `sendMessage`.

**The mctl-gitops side is not part of this change.** The
`mctl-agents-scout` ClusterWorkflowTemplate and a CronWorkflow
(`schedule: "40 6 * * *"`, UTC, `concurrencyPolicy: Forbid`, `suspend: true`)
must be added there; the manifest validator fails CI until the CWFT exists.
Env: `SCOUT_TELEGRAM_BOT_TOKEN`, `SCOUT_TELEGRAM_CHAT_ID` (optional secretKeyRefs)
and the GitHub read token.

## Dry run

```bash
python -m orchestrator.run_scout --dry-run
```

Prints the message and every button URL. No Telegram secrets are needed and
nothing is sent.

## Unsuspend checklist

1. Verify each feed URL in `sources.yaml` (the initial URLs were not checked).
2. Run the dry run and review the digest and footer.
3. Run one non-dry one-shot with the secrets set and confirm delivery.
4. Set `suspend: false` on the CronWorkflow.

## Rules

- Never call `getUpdates`, `deleteWebhook` or `setWebhook` on @MCTL_AI_bot. Its
  webhook belongs to mctl-agent; the scout calls `sendMessage` only.
- Add a source: append to `sources.yaml`. Disable one: `enabled: false` plus a `note`.
- Tune ranking: edit `ranking.weights` and per-source `weight`.
- Model tiers: `scout_relevance` (cheap) and `scout_scoring` (balanced) in
  `config/model-policy.yaml`. Set `scout_relevance: balanced` to fall back.
- Budget: `SCOUT_BUDGET_USD` (default 1.00) per run.

## Reading the footer

Each source shows `ok(n)`, `failed(reason)`, `malformed` or `disabled`. A failed
source is never a quiet news day. `dedupe: unavailable` means the `spike:scout`
search failed and duplicates may appear.
