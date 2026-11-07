# Temporal patch-marker scan

`tools/temporal_marker_scan.py` (MS-4) is a read-only check of which workflow
histories record a given Temporal patch marker. Tracker: mctl-agents#1007.

## Choosing the patch id

One run scans one patch id. The default is `deferred-resume-contract`; pick
another with `--patch-id`. The text and JSON reports name the id that was
scanned (`patch=...` / `"patch"`), so reports for several ids are separate runs.

```
python tools/temporal_marker_scan.py --address HOST:7233 --namespace NS \
    --patch-id merge-watch-terminal-reconcile --json
```

Verdicts and exit codes are the same for every id: `MARKER_PRESENT`,
`MARKER_ABSENT` (only for a complete, contiguous history from event 1) and
`UNKNOWN` (undecodable, gap, empty, listing incomplete; blocks with exit 2).

## Patches recorded only in the first run of a watch

Some patches are recorded once per fresh watch history and not again after a
continue-as-new hop. For those, the marker has to be looked for in the FIRST
run of the chain. The scanner already walks `continued_from` back to it, so a
hopped run reports `MARKER_PRESENT` when any ancestor run recorded the marker.

`merge-watch-terminal-reconcile` is of this kind. No build carries it yet, so
today every history scanned for it is `MARKER_ABSENT` by construction. That is a
point-in-time result about what has been recorded so far, not evidence that a
rollout is safe. Read it together with the `UNKNOWN` count, the retention gap
count and `listing_complete`.
