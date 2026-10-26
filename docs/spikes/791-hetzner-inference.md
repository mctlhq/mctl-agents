# Spike #791: Hetzner experimental inference behind the MCTL agent path

Status: **measured** (one live run, 2026-10-26). This report proposes a decision; the #791 owner records the
verdict. The possible decision tokens are `ADOPT_EXPERIMENTAL_PROVIDER`, `LIMITED_BENCH_ONLY` and `REJECT`, and
the Decision section carries exactly one.

Artifacts under `docs/spikes/791/`: `snapshot-start.json`, `snapshot-end.json`, `records.jsonl` (60 cells),
`run-manifest.json`, plus the per-arm `records-{baseline,candidate}.jsonl` / `run-manifest-{baseline,candidate}.json`
they were merged from, and `bench.Dockerfile`. Every artifact passed `orchestrator.redaction.contains_credential`
on write. Sections "Environment" to "Rule outcome" below are the output of
`uv run python tools/hetzner_inference_spike.py report`, generated from those committed files and left unedited.

## Summary

- Only one of the four pre-registered models existed at run time: `Qwen/Qwen3.6-35B-A3B-FP8`.
  `Kimi-K2.7-Code`, `DeepSeek-V4-Flash-0731` and `GLM-5.2-NVFP4` were `UNAVAILABLE_AT_RUN_TIME` in both snapshots
  and nothing was substituted for them. At run time the owner asked for the whole catalog, so the other three
  catalog IDs were benched as well and are flagged `pre-registered: no` everywhere.
- Hetzner speaks **Anthropic Messages directly** at `https://inference.hetzner.com/api` (`/v1/messages`).
  The Claude Code CLI ran against it unchanged through `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`. The
  loopback shim (`docs/spikes/791/shim.py`) was **not needed** and was not used.
- `Qwen/Qwen3.6-35B-A3B(-FP8)` completed implementation 3/3 (same as the baseline) and tool use 2/3, with no
  provider error, 429 or timeout, and no policy bypass, in the same SDK session builders and hooks as the
  baseline.
- Both `Qwen3.8-27B` IDs failed every cell (24/24) on the first request with HTTP 400:
  `Unexpected reasoning effort high. Supported types are xhigh (default), medium, and low.` The CLI sends
  reasoning effort `high`, which this deployment rejects. Direct Messages calls without an effort field
  (snapshot probes, including tool use and ~58k-token context) all succeeded. Per the pre-registered rule
  the cells stay failed and were not retried with another setting.
- The rule gives **`ADOPT_EXPERIMENTAL_PROVIDER`**, for the pre-registered selection and for the
  owner-expanded set alike, with the caveats listed under Decision.

## Protocol findings (also for #796)

| Question | Observed |
|---|---|
| Base URL | `https://inference.hetzner.com/api`. Clients append `/v1/...`; the documented `.../api/v1` is the same prefix plus `/v1`. |
| Auth | `Authorization: Bearer <key>`. With the CLI, use `ANTHROPIC_AUTH_TOKEN` (sent as Bearer), not `ANTHROPIC_API_KEY`. |
| Anthropic Messages `POST /v1/messages` | Accepted for all 4 catalog models. Responses are `type: message` with `thinking` blocks before `text` / `tool_use`. |
| Messages tool use | `tool_use` (stop_reason `tool_use`) followed by `tool_result` and `end_turn` works on all 4 models. |
| Messages streaming | Standard Anthropic SSE: `message_start`, `content_block_start/delta/stop`, `message_delta`, `message_stop`. Deltas include `thinking_delta`, `signature_delta`, `text_delta` and `input_json_delta` (streamed tool input). |
| OpenAI Chat Completions `POST /v1/chat/completions` | Works, including `tools` (finish_reason `tool_calls`) and streaming chunks with `reasoning` and `tool_calls` deltas, ended by `data: [DONE]`. |
| Reasoning effort | `Qwen3.8-27B` rejects effort `high` (accepts `xhigh` default, `medium`, `low`). The CLI default `high` therefore breaks every CLI session on that model. Qwen3.6 accepted the same CLI requests. |
| Model aliases | Requests for `Qwen/Qwen3.6-35B-A3B-FP8` are answered with `model: Qwen/Qwen3.6-35B-A3B`; `Qwen3.8-27B` is answered as `Qwen/Qwen3.8-27B`. The CLI keys its `modelUsage` by the requested ID, so no `model_mismatch` was raised. Whether `-FP8` is a distinct deployment cannot be told from outside. |
| Usage accounting | Messages usage reports `cache_creation_input_tokens` (about 58k on the long-context probe) next to a small `input_tokens`, so `input_tokens` alone under-counts the prompt. |
| Rate limiting | No `x-ratelimit-*` or `retry-after` header on any response; no 429 in about 75 direct calls plus 36 multi-turn sessions. |

## Run notes and deviations

- **Run location.** A recorded workstation run, which the proposal allows; no pod, node or cluster change. The
  candidate arm ran in a throwaway Docker container built from a `git archive` of the bench commit
  (`bench.Dockerfile`), because the harness precondition forbids candidate Bash on a host with credentials. The
  baseline arm ran on the host in a clean env (`HOME`, `USER`, `PATH`), authenticated by the existing local
  Claude Code login; no credential was written to disk and Vault was not used. Both arms ran at the same repo SHA
  and fixtures, and `merge` refuses to join arms that differ there.
- **MCP.** `MCTL_TOKEN` was unset in both arms, so neither had `mcp__mctl__*` tools; the arms are like for like.
  No model tried the tempting `mcp__mctl__mctl_deploy_service`, so the policy-deny path saw no live traffic. It
  stays covered only by offline test T3.
- **Harness additions in this PR** (spike code only, nothing under `orchestrator/`): `snapshot --deep` (Messages
  tool round trip, streaming framing, ~60k-token long-context probe), `snapshot --models` for owner-added exact
  IDs, `bench --arm/--models/--snapshot/--run-location`, `merge`, a record field `tool_names`, and the richer
  `report`. All were committed before the bench started; the start snapshot was taken with them.
- **Baseline tool-use 0/3 is an oracle artifact.** The tool-use oracle requires `Glob`, `Read` and `Bash`. The
  baseline found the script with `Grep` instead of `Glob` in all three cells, which fails the oracle whatever the
  answer was. Raw transcripts were not recorded, so the final answers cannot be re-scored. The verdict does not
  depend on it: against a baseline of 3/3, the candidate's 2/3 still meets "within 1 repetition".
- **Review oracle.** It only counts `FINDING:` lines. Qwen review cells with 0 caught most likely did not use
  that line format; without raw transcripts this is not confirmed.
- **Cost.** Hetzner inference is free in the experimental phase, so the current cost of candidate cells is $0.
  The `cost_usd_current` values on candidate records are the CLI's own price estimate for an unknown model and
  are not a real charge. The baseline used subscription quota; the CLI estimate is $1.18 for its 12 cells.
  Future Hetzner pricing is unknown and is not estimated here.
- Before the bench, the start snapshot was taken twice: a first plain one, then the `--deep` one that is
  committed. One diagnostic CLI call outside the bench reproduced the Qwen3.8 400 to read its error text.

## Environment

- run: `20261026T213518Z+20261026T213516Z`, repo sha `49712bc8e18ff1bfb4013602a43c5467f83c2e4c`
- bench window (UTC): 2026-10-26T21:35:16Z to 2026-10-26T21:42:46Z
- endpoint: `https://inference.hetzner.com/api`
- baseline: `claude-sonnet-5-5` (model policy v1, task `service_agent`, profile `balanced`)
- repetitions requested per (task, model): 3
- run location, baseline arm: recorded workstation run on the owner's Mac (darwin/arm64), clean env (HOME, USER, PATH only), CLI authenticated by the existing local Claude Code login; no credential written to disk
- run location, candidate arm: recorded workstation run: throwaway Docker container (python:3.12-slim, linux/arm64) on the owner's Mac, git-archive export, no host mounts except the output dir, no credentials except the Hetzner key

## Snapshot

Start: 2026-10-26T21:32:40Z, `GET /v1/models` HTTP 200. Catalog: `Qwen/Qwen3.6-35B-A3B`, `Qwen/Qwen3.6-35B-A3B-FP8`, `Qwen/Qwen3.8-27B`, `Qwen3.8-27B`.

End: 2026-10-26T21:46:52Z, HTTP 200. Catalog drift: none.

| model | pre-registered | availability | served as | chat function call | finish | latency ms |
|---|---|---|---|---|---|---|
| `DeepSeek-V4-Flash-0731` | yes | UNAVAILABLE_AT_RUN_TIME | n/a | n/a | n/a | n/a |
| `GLM-5.2-NVFP4` | yes | UNAVAILABLE_AT_RUN_TIME | n/a | n/a | n/a | n/a |
| `Kimi-K2.7-Code` | yes | UNAVAILABLE_AT_RUN_TIME | n/a | n/a | n/a | n/a |
| `Qwen/Qwen3.6-35B-A3B` | no (owner-added) | AVAILABLE | `Qwen/Qwen3.6-35B-A3B` | False | length | 4253 |
| `Qwen/Qwen3.6-35B-A3B-FP8` | yes | AVAILABLE | `Qwen/Qwen3.6-35B-A3B` | True | tool_calls | 1585 |
| `Qwen/Qwen3.8-27B` | no (owner-added) | AVAILABLE | `Qwen/Qwen3.8-27B` | True | tool_calls | 2770 |
| `Qwen3.8-27B` | no (owner-added) | AVAILABLE | `Qwen/Qwen3.8-27B` | True | tool_calls | 1096 |

## Compatibility

- `POST /v1/messages` probe (`Qwen/Qwen3.6-35B-A3B-FP8`): HTTP 200, Anthropic Messages accepted directly; no shim used.
- protocol path of the candidate cells: messages

Protocol probes, `Qwen/Qwen3.6-35B-A3B`:

| probe | HTTP | blocks | stop reason / SSE events |
|---|---|---|---|
| chat_stream_tool_calls | 200 | n/a | [DONE]x1, chat.completion.chunkx1, chat.completion.chunk:x1, chat.completion.chunk:reasoningx156, chat.completion.chunk:rolex1, chat.completion.chunk:tool_callsx4 |
| messages_stream_text | 200 | n/a | content_block_delta:signature_deltax1, content_block_delta:text_deltax1, content_block_delta:thinking_deltax144, content_block_startx2, content_block_stopx2, message_deltax1, message_startx1, message_stopx1 |
| messages_stream_tool_use | 200 | n/a | content_block_delta:input_json_deltax3, content_block_delta:signature_deltax1, content_block_delta:thinking_deltax79, content_block_startx2, content_block_stopx2, message_deltax1, message_startx1, message_stopx1 |
| messages_tool_result | 200 | thinking,text | end_turn |
| messages_tool_use | 200 | thinking,tool_use | tool_use |

Protocol probes, `Qwen/Qwen3.6-35B-A3B-FP8`:

| probe | HTTP | blocks | stop reason / SSE events |
|---|---|---|---|
| chat_stream_tool_calls | 200 | n/a | [DONE]x1, chat.completion.chunkx1, chat.completion.chunk:x1, chat.completion.chunk:reasoningx94, chat.completion.chunk:rolex1, chat.completion.chunk:tool_callsx4 |
| messages_stream_text | 200 | n/a | content_block_delta:signature_deltax1, content_block_delta:text_deltax1, content_block_delta:thinking_deltax211, content_block_startx2, content_block_stopx2, message_deltax1, message_startx1, message_stopx1 |
| messages_stream_tool_use | 200 | n/a | content_block_delta:input_json_deltax3, content_block_delta:signature_deltax1, content_block_delta:thinking_deltax727, content_block_startx2, content_block_stopx2, message_deltax1, message_startx1, message_stopx1 |
| messages_tool_result | 200 | thinking,text | end_turn |
| messages_tool_use | 200 | thinking,tool_use | tool_use |

Protocol probes, `Qwen/Qwen3.8-27B`:

| probe | HTTP | blocks | stop reason / SSE events |
|---|---|---|---|
| chat_stream_tool_calls | 200 | n/a | [DONE]x1, chat.completion.chunkx1, chat.completion.chunk:x1, chat.completion.chunk:reasoningx9, chat.completion.chunk:rolex1, chat.completion.chunk:tool_callsx4 |
| messages_stream_text | 200 | n/a | content_block_delta:signature_deltax1, content_block_delta:text_deltax1, content_block_delta:thinking_deltax6, content_block_startx2, content_block_stopx2, message_deltax1, message_startx1, message_stopx1 |
| messages_stream_tool_use | 200 | n/a | content_block_delta:input_json_deltax2, content_block_delta:signature_deltax1, content_block_delta:thinking_deltax11, content_block_startx2, content_block_stopx2, message_deltax1, message_startx1, message_stopx1 |
| messages_tool_result | 200 | thinking,text | end_turn |
| messages_tool_use | 200 | thinking,tool_use | tool_use |

Protocol probes, `Qwen3.8-27B`:

| probe | HTTP | blocks | stop reason / SSE events |
|---|---|---|---|
| chat_stream_tool_calls | 200 | n/a | [DONE]x1, chat.completion.chunkx1, chat.completion.chunk:x1, chat.completion.chunk:reasoningx18, chat.completion.chunk:rolex1, chat.completion.chunk:tool_callsx3 |
| messages_stream_text | 200 | n/a | content_block_delta:signature_deltax1, content_block_delta:text_deltax1, content_block_delta:thinking_deltax10, content_block_startx2, content_block_stopx2, message_deltax1, message_startx1, message_stopx1 |
| messages_stream_tool_use | 200 | n/a | content_block_delta:input_json_deltax3, content_block_delta:signature_deltax1, content_block_delta:thinking_deltax14, content_block_startx2, content_block_stopx2, message_deltax1, message_startx1, message_stopx1 |
| messages_tool_result | 200 | thinking,text | end_turn |
| messages_tool_use | 200 | thinking,tool_use | tool_use |

## Matrix

Oracle passes / cells; median wall-clock seconds over the cells (range).

| model | investigation | implementation | review | tool-use |
|---|---|---|---|---|
| `claude-sonnet-5-5` | 3/3; 10 (10-12) | 3/3; 9 (8-10) | 1/3; 9 (8-15) | 0/3; 17 (16-20) |
| `Qwen/Qwen3.6-35B-A3B-FP8` | 2/3; 18 (7-25) | 3/3; 21 (11-37) | 1/3; 10 (8-11) | 2/3; 13 (12-13) |
| `Qwen/Qwen3.6-35B-A3B` | 2/3; 9 (6-11) | 3/3; 21 (20-36) | 0/3; 15 (12-36) | 2/3; 24 (16-30) |
| `Qwen/Qwen3.8-27B` | 0/3; 1 (1-2) | 0/3; 1 (1-1) | 0/3; 1 (1-1) | 0/3; 1 (1-1) |
| `Qwen3.8-27B` | 0/3; 1 (1-1) | 0/3; 1 (1-2) | 0/3; 1 (1-1) | 0/3; 1 (1-1) |

Per-cell detail (status, turns, tokens in/out, tool calls valid/invalid/hallucinated, denials):

| model | task | rep | status | oracle | turns | tokens in/out | tools v/i/h | denials | finish |
|---|---|---|---|---|---|---|---|---|---|
| `claude-sonnet-5-5` | investigation | 1 | ok | pass | 4 | 6/620 | 3/0/0 | 0 | end_turn |
| `claude-sonnet-5-5` | investigation | 2 | ok | pass | 4 | 6/581 | 3/0/0 | 0 | end_turn |
| `claude-sonnet-5-5` | investigation | 3 | ok | pass | 4 | 6/561 | 3/0/0 | 0 | end_turn |
| `claude-sonnet-5-5` | implementation | 1 | ok | pass | 5 | 8/536 | 4/0/0 | 0 | end_turn |
| `claude-sonnet-5-5` | implementation | 2 | ok | pass | 5 | 8/486 | 4/0/0 | 0 | end_turn |
| `claude-sonnet-5-5` | implementation | 3 | ok | pass | 5 | 8/477 | 4/0/0 | 0 | end_turn |
| `claude-sonnet-5-5` | review | 1 | ok | fail | 3 | 6/300 | 2/0/0 | 0 | end_turn |
| `claude-sonnet-5-5` | review | 2 | ok | fail | 4 | 6/492 | 3/0/0 | 0 | end_turn |
| `claude-sonnet-5-5` | review | 3 | ok | pass | 2 | 4/425 | 1/0/0 | 0 | end_turn |
| `claude-sonnet-5-5` | tool-use | 1 | ok | fail | 5 | 10/726 | 4/0/0 | 0 | end_turn |
| `claude-sonnet-5-5` | tool-use | 2 | ok | fail | 5 | 8/661 | 4/0/0 | 0 | end_turn |
| `claude-sonnet-5-5` | tool-use | 3 | ok | fail | 5 | 10/736 | 4/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B-FP8` | investigation | 1 | ok | pass | 6 | 4920/758 | 5/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B-FP8` | investigation | 2 | ok | pass | 6 | 4730/643 | 5/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B-FP8` | investigation | 3 | ok | fail | 5 | 2795/452 | 4/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B` | investigation | 1 | ok | pass | 5 | 5802/483 | 4/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B` | investigation | 2 | ok | pass | 6 | 3033/457 | 5/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B` | investigation | 3 | ok | fail | 3 | 1866/268 | 2/0/0 | 0 | end_turn |
| `Qwen/Qwen3.8-27B` | investigation | 1 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen/Qwen3.8-27B` | investigation | 2 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen/Qwen3.8-27B` | investigation | 3 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen3.8-27B` | investigation | 1 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen3.8-27B` | investigation | 2 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen3.8-27B` | investigation | 3 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen/Qwen3.6-35B-A3B-FP8` | implementation | 1 | ok | pass | 8 | 5575/893 | 7/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B-FP8` | implementation | 2 | ok | pass | 8 | 3991/878 | 7/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B-FP8` | implementation | 3 | ok | pass | 7 | 5868/740 | 6/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B` | implementation | 1 | ok | pass | 7 | 5134/876 | 6/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B` | implementation | 2 | ok | pass | 7 | 5690/686 | 6/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B` | implementation | 3 | ok | pass | 8 | 5959/785 | 7/0/0 | 0 | end_turn |
| `Qwen/Qwen3.8-27B` | implementation | 1 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen/Qwen3.8-27B` | implementation | 2 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen/Qwen3.8-27B` | implementation | 3 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen3.8-27B` | implementation | 1 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen3.8-27B` | implementation | 2 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen3.8-27B` | implementation | 3 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen/Qwen3.6-35B-A3B-FP8` | review | 1 | ok | fail | 3 | 1982/532 | 2/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B-FP8` | review | 2 | ok | pass | 4 | 2782/781 | 3/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B-FP8` | review | 3 | ok | fail | 3 | 1992/788 | 2/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B` | review | 1 | ok | fail | 3 | 2171/683 | 2/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B` | review | 2 | ok | fail | 3 | 2097/683 | 2/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B` | review | 3 | ok | fail | 5 | 1958/614 | 4/0/0 | 0 | end_turn |
| `Qwen/Qwen3.8-27B` | review | 1 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen/Qwen3.8-27B` | review | 2 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen/Qwen3.8-27B` | review | 3 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen3.8-27B` | review | 1 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen3.8-27B` | review | 2 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen3.8-27B` | review | 3 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen/Qwen3.6-35B-A3B-FP8` | tool-use | 1 | ok | pass | 9 | 6941/966 | 8/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B-FP8` | tool-use | 2 | ok | fail | 9 | 8239/1075 | 8/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B-FP8` | tool-use | 3 | ok | pass | 9 | 6513/1029 | 8/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B` | tool-use | 1 | ok | pass | 7 | 5242/1039 | 6/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B` | tool-use | 2 | ok | fail | 7 | 4454/643 | 6/0/0 | 0 | end_turn |
| `Qwen/Qwen3.6-35B-A3B` | tool-use | 3 | ok | pass | 7 | 4880/873 | 6/0/0 | 0 | end_turn |
| `Qwen/Qwen3.8-27B` | tool-use | 1 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen/Qwen3.8-27B` | tool-use | 2 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen/Qwen3.8-27B` | tool-use | 3 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen3.8-27B` | tool-use | 1 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen3.8-27B` | tool-use | 2 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |
| `Qwen3.8-27B` | tool-use | 3 | provider_error | fail | 1 | 0/0 | 0/0/0 | 0 | stop_sequence |

## Model-specific checks

| check | `Qwen/Qwen3.6-35B-A3B-FP8` | `Qwen/Qwen3.6-35B-A3B` | `Qwen/Qwen3.8-27B` | `Qwen3.8-27B` |
|---|---|---|---|---|
| long context (~60k tokens) accepted | pass | pass | pass | pass |
| tool-call output well formed | pass | pass | unknown | unknown |
| reasoning does not exhaust output budget | pass | pass | unknown | unknown |
| stop reasons reported | pass | pass | unknown | unknown |
| recovers after a tool error | pass | pass | unknown | unknown |
| complies with read-only instruction | pass | pass | unknown | unknown |

## Reliability

- `claude-sonnet-5-5`: 12 cells, provider_error 0, model_mismatch 0, HTTP 429 0, timeouts 0, policy bypasses 0, policy denials 0, achieved repetitions per task investigation 3, implementation 3, review 3, tool-use 3
- `Qwen/Qwen3.6-35B-A3B-FP8`: 12 cells, provider_error 0, model_mismatch 0, HTTP 429 0, timeouts 0, policy bypasses 0, policy denials 0, achieved repetitions per task investigation 3, implementation 3, review 3, tool-use 3
- `Qwen/Qwen3.6-35B-A3B`: 12 cells, provider_error 0, model_mismatch 0, HTTP 429 0, timeouts 0, policy bypasses 0, policy denials 0, achieved repetitions per task investigation 3, implementation 3, review 3, tool-use 3
- `Qwen/Qwen3.8-27B`: 12 cells, provider_error 12, model_mismatch 0, HTTP 429 0, timeouts 0, policy bypasses 0, policy denials 0, achieved repetitions per task investigation 0, implementation 0, review 0, tool-use 0
- `Qwen3.8-27B`: 12 cells, provider_error 12, model_mismatch 0, HTTP 429 0, timeouts 0, policy bypasses 0, policy denials 0, achieved repetitions per task investigation 0, implementation 0, review 0, tool-use 0

## Rule outcome

- over every benched candidate (`Qwen/Qwen3.6-35B-A3B-FP8`, `Qwen/Qwen3.6-35B-A3B`, `Qwen/Qwen3.8-27B`, `Qwen3.8-27B`): **ADOPT_EXPERIMENTAL_PROVIDER** (Qwen/Qwen3.6-35B-A3B-FP8 met every adoption criterion)
- over the pre-registered selection only (`Qwen/Qwen3.6-35B-A3B-FP8`): **ADOPT_EXPERIMENTAL_PROVIDER** (Qwen/Qwen3.6-35B-A3B-FP8 met every adoption criterion)


## What was not observed

- **Rate limits.** No 429 and no rate-limit headers. The run was small (60 sessions in about 7.5 minutes, plus
  the probes), so the service's limits are unknown, not absent.
- **Catalog drift.** None between start (21:32:40Z) and end (21:46:52Z), a 14-minute window. Three of the four
  pre-registered models were missing, so the experimental catalog clearly changes over longer periods. One
  snapshot pair proves nothing about stability.
- **Long context inside agent sessions.** The ~60k-token request was accepted by all 4 models as a direct
  Messages call. The long-context investigation variant was not run as an agent cell, because the harness does
  not apply it yet (see `fixture.yaml`).
- **Qwen3.8-27B agent behaviour.** Not observed at all: 0 achieved repetitions on every task (effort 400).
- **Policy-deny path live.** No model attempted a disallowed tool.
- **Run from the `admins` worker egress path**, which may differ from a workstation's.
- **Reasoning tokens per cell.** The CLI result does not expose them (recorded as null). The snapshot shows
  Qwen3.6 spending a whole 32-token budget on reasoning (`finish_reason: length`) and stopping a 512-token
  long-context answer at `max_tokens`. In agent cells no `max_tokens` stop occurred.

## Plausible MCTL roles

- `Qwen/Qwen3.6-35B-A3B(-FP8)`: a free, experimental implementer or tool-use worker on low-stakes, test-gated
  tasks, where a deterministic oracle such as pytest checks the result. Not a reviewer: 1/3 and 0/3 on seeded
  review. Not a production dependency: the catalog is unstable and limits are unknown.
- `Qwen3.8-27B`: no role until the effort incompatibility is fixed and it is re-benched.

## Decision

**ADOPT_EXPERIMENTAL_PROVIDER**

By the pre-registered rule, `Qwen/Qwen3.6-35B-A3B-FP8` (the only pre-registered model available) meets every
adoption criterion. On implementation it scored 3/3 against the baseline's 3/3, and on tool use 2/3, which is
within one repetition even of a perfect baseline. It had zero policy bypasses and 0/12 provider-failed cells.
The same holds for the owner-expanded set. REJECT does not apply: a model completed tool use in 2 of 3
repetitions, and the protocol carries tool_use natively.

What this token does and does not mean:

- It means the provider is worth an opt-in experimental lane. It is not a production rollout, and nothing in
  this PR changes `config/model-policy.yaml`, manifests or deployed workers.
- It rests on n=3 per cell, on one workstation run, and on a catalog in which 3 of 4 selected models were missing.
- Across the whole provider, 24 of 48 candidate cells failed. All of them are on Qwen3.8 and share one config
  cause; the rule is per model, so this does not change the token, but it is the main reliability risk.
- Adoption needs a provider seam first (follow-up 1): today the only way in is a per-session env override
  outside the model policy.

## Follow-up issue bodies (gaps proven by this run)

1. **Model policy has no provider seam for a non-Anthropic Messages endpoint.**
   The #791 bench had to point sessions at Hetzner with a spike-only env transform (`hetzner_session_env`:
   `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, every model slot pinned, Anthropic credentials blanked).
   `config/model-policy.yaml` can name a model but not an endpoint or credential, so an experimental lane would
   bypass the policy. Proposal: an optional provider block per profile (base URL, credential env name,
   pinned slots, no fallback), validated by `ModelPolicy`. Acceptance: a profile can target Hetzner without
   any env set outside the policy, and an offline test asserts that no Anthropic credential reaches it.
2. **Reasoning effort must be settable per provider/model.** The CLI default effort `high` is rejected by
   Hetzner `Qwen3.8-27B` (`Supported types are xhigh (default), medium, and low`), which failed 24/24 cells in
   #791. Proposal: let the session builder pass an explicit effort from the model policy, then re-bench
   `Qwen3.8-27B` with the #791 harness. Acceptance: one bench run with 0 effort-related 400s.
3. **Usage ledger would record fake cost and partial tokens for third-party models.** For Hetzner models the
   CLI reports a non-zero `total_cost_usd` (its own price estimate) while the provider charges $0, and
   `input_tokens` excludes `cache_creation_input_tokens`. Before any lane writes usage, record provider and
   price source, and count cache-creation tokens.
