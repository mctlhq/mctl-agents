# Qwen row for #796 (Hetzner inference, 2026-10-27/28)

- Model: `Qwen/Qwen3.6-35B-A3B-FP8`, served by Hetzner inference through its
  Anthropic-compatible endpoint (`ANTHROPIC_BASE_URL=https://inference.hetzner.com/api`).
  Every number in this directory comes from Hetzner; Nebius was not used (its
  API is OpenAI-compatible only and has no `/v1/messages`).
- Harness: `tools/rea_spike.py` at `7afb04b`, same REA 6.8.0 install and fixtures
  as run 40533093166, executed in a throwaway workstation container instead of the
  `rea-spike-796` workflow (the workflow has no alternate-provider input).
  `fixture-digests.txt` holds the sha256 of the two built fixtures.
- Required setting: `ANTHROPIC_DEFAULT_SONNET_MODEL=Qwen/Qwen3.6-35B-A3B-FP8`.
  Claude Code CLI 2.1.301 sends compound Bash commands to an auto-mode permission
  classifier on the `claude-sonnet-5` alias over the same base URL. Hetzner does
  not serve that model, so without the mapping every compound Bash command is
  denied and the session loops until killed (the harness sets no `max_turns`).
  `CLAUDE_CODE_AUTO_MODE_MODEL` had no effect. With the mapping the classifier runs
  on Qwen itself.
- Limits: 1200 s per investigation cell, 3000 s per probe, one attempt each.
  A cell that hit the limit is recorded as `infra-failure` with `rc: 124`.
- Grading: `grading.yaml`, same rules as the Claude run.
