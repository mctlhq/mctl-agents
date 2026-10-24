# Spike #757 prototype: AttentionDecision v1 and decide()

Fixture-only. Nothing here is imported by `orchestrator/` or deployed.

Run all measurements and tests with one command:

```sh
uv run pytest tests/test_spike_757_attention.py -q
```

- `envelope_v1.py`: cited copy of the closed `mctl.events/v1` shape. Source:
  mctlhq/mctl-claude-remote `events/mctl_events/envelope.py` at
  `989bff3b8c0da53cfb109110dfadb022de142e40`; canonical schema mctlhq/.github
  `events/schemas/event-envelope.v1.schema.json` at
  `92b1e28327b0d0b575b18007ab6935d45dff057d`.
- `attention.py`: `AttentionDecision` (`attentiondecision/v1`), `ObservedState`,
  `decide()`, `decide_wake_cycle()`. Stdlib plus PyYAML; no I/O, no approval client.
- `rules/github_pr.yaml`: first-match rules.
- `fixtures/*.json`: an envelope plus a separate `observed` state. The envelope
  never carries CI, mergeability or review severity; `observed` stands in for the
  re-observation that `channel.py` would do and is fixture-only.
- `test_attention.py`: scenario and mutation tests, collected by
  `tests/test_spike_757_attention.py`.

The subject keys used by the fixtures (`repository`, `number`) are assumed from the
deployed route policy (`subject.repository: mctlhq/*`); the producer's exact GitHub
subject keys were not read in this run.
