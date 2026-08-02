# 0001 — Keep mctl-agents on Python

**Status:** proposed
**Date:** 2026-08-02

## Context

`mctl-agents` is a batch orchestrator of LLM sessions, built on
`claude-agent-sdk`. The question was raised whether it should be ported to a
more performant language (Go, Rust, Java).

## Decision

`mctl-agents` stays on Python.

## Drivers

1. **The Agent SDK is available for Python and TypeScript only.** Anthropic's
   documentation states this directly: the SDK is a library for those two
   languages, and driving the same agent loop from another language means
   running the CLI as a subprocess.

   The constraint is **economic, not absolute**. A port is technically
   possible, but it would mean maintaining a substantial part of the agent
   harness ourselves on top of the CLI — hooks, permissions, sessions,
   subagents, MCP wiring, streaming-JSON parsing, budgets — and then chasing
   upstream indefinitely.

   Note the distinction: the API SDK (`anthropic`) exists for seven
   languages, but it is a different product. It provides `messages.create()`,
   not an agent loop with built-in tools.

2. **There is no evidence that Python is the bottleneck.** No profiling has
   been done, and this decision does not claim otherwise. By the nature of
   the work, latency is dominated by external services and subprocesses:
   LLM inference, the `gh` CLI, `git clone`, HTTP calls to the mctl MCP
   server. The architecture already runs agents concurrently via AnyIO.

   Concurrency being present does not prove the absence of other limits —
   API quotas, blocking subprocess calls, workspace contention, file
   descriptors, rate limiting. If throughput becomes a problem, that is
   where to look, not at the language.

3. **Cost is driven by tokens, not compute.** The levers are model routing,
   per-agent budgets, effort, and prompt caching.

4. **The organisation's language split is already correct.** Go is used for
   network services (mctl-api, mctl-telegram, mctl-agent). `mctl-agents` is
   a batch orchestrator, where Python is a reasonable fit.

## Consequences

- The Python tooling must be brought up to the standard of the other
  repositories: reproducible environments, a mandatory CI gate, a linter,
  and type checking. This is tracked as a separate execution plan.
- The harness version is determined by the pinned `claude-agent-sdk`
  version. Upgrading it is a deliberate, standalone change.
- Type strictness in Python is opt-in, so mypy is required where TypeScript
  would have provided checking for free.

## Rejected alternatives

### Go / Rust / Java

Loses the Agent SDK (Driver 1). Not economically justifiable.

### TypeScript

The only genuine alternative: it has a full Agent SDK with the same agent
loop, tools, hooks, sessions, MCP, permissions, and subagents. The "no SDK"
argument does not apply here, so the decision rests on other grounds.

TypeScript is objectively better in two respects. First, it is likely the
leading implementation — the Python SDK changelog regularly describes
features as reaching parity with TypeScript. Second, it checks discriminated
unions at compile time, and the Agent SDK has many of them: message types,
content blocks, hook inputs and outputs, permission results, MCP
configuration, session lifecycle events.

Neither advantage addresses this repository's actual constraints.
Performance is bounded by the LLM, the network, and subprocesses; cost is
bounded by tokens. Porting the existing code and test suite would, in the
near term, *reduce* reliability: cancellation semantics, subprocess
management, and timeout and exception paths would all change, and hooks,
permissions, and MCP integrations would need revalidating.

The decision is therefore not "TypeScript is a poor choice" but **"TypeScript
is technically suitable, yet migrating does not create value commensurate
with its cost and risk"**. For a greenfield orchestrator, choosing
TypeScript would be defensible.

### asyncio optimisations, pooling

Optimising an unproven bottleneck.

## Revisit criteria

This decision holds **until there is a measurable reason to reconsider it**.
Revisit if:

- the Agent SDK ships for another language;
- profiling shows a significant share of CPU time inside Python;
- the orchestrator becomes a long-running, high-throughput service;
- startup time or memory consumption become a measurable infrastructure
  problem;
- a shared runtime with another core component is required;
- *(TypeScript specifically)* a needed SDK feature stays absent from Python
  for an extended period; a significant part of the orchestration moves onto
  an existing TypeScript platform; or maintaining the Python SDK starts
  blocking releases.
