# Spike 556: Jev vs Clef-flash as advisory semantic guardrails

Issue: mctlhq/mctl-agents#556. Status: replay-only spike, no live provider call, no spend.
Harness: `tools/semantic_decision_spike.py`. Fixtures: `tests/fixtures/semantic_decision/`.
Tests: `tests/test_semantic_decision_spike.py`.

Recommendation: NO_GO

NO_GO-pending: live behaviour not measured

## Scope and honesty about the data

The owner decided on 2026-10-23 that this spike is replay-only: no provider credentials
exist, nobody owns the vendor accounts or their spend. So every vendor-specific number
(latency, accuracy, stability, calibration, cost, model version) is **not measured**. The
vendor facts quoted in the issue (prices, 24,576-token context, vendor latency figures) were
not checked against either vendor; the Jev and Clef-flash request schemas in the harness are
assumptions recorded in `request_schema` and must be verified at the live run.

What this spike does produce:

- the `semantic-decision/v1alpha1` request/result contract and the authority combinator;
- 45 sanitized, synthetic fixtures (scenarios A 15, B 10, C 9, D 11; 11 are `danger: true`
  with a deterministic ALLOW; 8 minimal pairs; adversarial and model-claim cases);
- a run and report pipeline, proved offline on two synthetic replay providers
  (`replay-a`, `replay-b`). Their answers are authored by the harness, so their scores say
  nothing about Jev or Clef-flash. `golden/report.md` shows the report shape only.
- the deterministic-only baseline: it leaves all 11 dangerous ALLOW fixtures unescalated.
  That is the gap a semantic signal could fill, and the only one.

Fixture labels are by the spike author (`labelled_by`). The second labeler required by the
proposal is a named human reviewer on the spike PR; until then `confirmed_by` carries the
placeholder `pending:spike-PR-reviewer` and every `label_status` is `unconfirmed`. Replace
both from the PR review; mark disagreements `contested`.

## Trust boundary and failure semantics

```text
STRICTNESS = {ALLOW: 0, REQUIRE_APPROVAL: 1, DENY: 2}
combine(det, result, threshold):
  det != ALLOW                      -> det
  signal_status != ok               -> det
  HIGH_RISK and confidence >= thr   -> REQUIRE_APPROVAL
  otherwise                         -> det
```

- A model can only turn ALLOW into REQUIRE_APPROVAL. It cannot produce ALLOW or DENY, set
  `Decision.permitted`, or satisfy CI, type-check, test or approval evidence.
- Timeout, error, malformed output and low confidence all return the deterministic verdict.
- Inputs are canonical `ActionRequest` fields plus bounded fixture metadata. Each string is
  at most 256 characters and screened by `redaction.contains_credential`. Tool descriptions
  and model claims go only into the delimited `untrusted` section.
- Prompts and completions are never persisted by default; `--record-raw DIR` writes them and
  refuses any directory inside a repository.
- Nothing under `orchestrator/` references the harness (tested).

## Audit and trace fields

Per call the JSONL record carries: provider, model, model_version, endpoint, request_schema,
input_tokens, latency_ms, http_status, retries, signal_status, recommendation, categories,
reason_codes, confidence, estimated_cost_usd, deterministic_verdict, simulated_verdict, and
the fixture's expected label. If an integration were ever adopted, `POLICY_DECISION` would add
`semantic.provider`, `semantic.model`, `semantic.recommendation`, `semantic.signal_status`
and `semantic.latency_ms`.

## Findings per scenario

- **A, risk classification.** The only cases a signal can change are the ALLOW-ruled ones
  (A04, A08, A12, A15). Each is expressible as a deterministic rule on `operation` or
  `target` (see candidates below), so the value over rules is unproven.
- **B, approval recommendation.** Every B fixture is already REQUIRE_APPROVAL. A signal can
  only annotate the approver's request; it cannot change the outcome. Annotation value
  needs a human-factors measurement this spike did not run.
- **C, evidence sufficiency.** Missing CI or review evidence is a deterministic check. The
  claim-versus-evidence pair (C04/C05) is decided by the evidence field alone, so a
  model adds nothing there.
- **D, DevLoop triage.** Explicit verdict: the dangerous D fixtures (D02, D03, D07, D11) are
  caught by path- and content-based rules and by the configured reviewers; D08 and D10 are
  what CI and review already cover. On this fixture set D duplicates CI, Ruff/Pyrefly and
  the reviewers, and no measurement suggests otherwise. Not shown to be worth a dependency.

## Answers to the ten questions

1. Improvement over rules to justify a dependency: not shown; not measured live.
2. Deterministic: operation, target, grants, evidence presence, paths. Semantic inference is
   only plausible for free-text context (descriptions, PR summaries), where it is also the
   most injectable.
3. Jev vs Clef-flash: not measured. By assumption alone, a Cloudflare REST call is one
   fewer vendor to onboard if Cloudflare is already used; this is not evidence.
4. Synchronous latency: not measured. The 300 ms p95 budget is an **owner-unconfirmed
   assumption**, chosen because the existing approval path allows about 20 s only for
   REQUIRE_APPROVAL actions. Do not use it as an acceptance criterion until confirmed.
5. Failure semantics: the deterministic verdict stands (see above); tested for timeout,
   error, malformed and low_confidence.
6. Safe inputs: canonical fields only, bounded, credential-screened, untrusted text
   delimited. No raw tool output, private prompts or credentials.
7. Offline reuse: yes. The fixture set, contract and pure `report` function are
   independent of any runtime decision and can seed .github#151 and #310.
8. Hosted vs open-weight: not assessed beyond paper; hosted privacy and availability are
   unmeasured. Self-hosting adds GPU or Workers-compatible serving to operate. Revisit only
   if a live run shows hosted Clef-flash fails on privacy or availability.
9. Calibration: not measured. The report computes a 5-bin table and Brier score from live
   records; replay values are shown as not measured.
10. Portability: the provider-neutral contract keeps vendor semantics inside the provider
    adapter; switching vendors needs a new adapter and a re-run, not a policy change.

## GO criteria check

| Criterion | Status |
|---|---|
| Measurable improvement on a class not better expressed as a rule | Not met: not measured, and every dangerous ALLOW fixture has a rule candidate |
| No path where the model relaxes deterministic policy | Met in the harness (monotone `combine`, tested) |
| Bounded, validated output | Met (closed vocabulary, malformed on violation) |
| Acceptable synchronous latency | Not measured |
| Safe behaviour on timeout and error | Met (tested against a stubbed transport) |
| Observability to explain signal and outcome | Met on paper (fields above) |
| Acceptable dangerous false-negative rate | Not measured |
| Operational complexity justified by value | Not met: no value measured |

No GO criterion with a measured outcome is satisfied, so the answer is NO_GO, qualified as
NO_GO-pending: live behaviour not measured. The qualifier is not a fifth token.

## Deterministic rule candidates (replace the model, simpler and safer)

These are candidates for a follow-up, each needing its own proposal and tests:

- `github.git.push` to a protected or default branch gets its own operation (for example
  `push:default-branch`) with DENY or REQUIRE_APPROVAL (A04).
- Credential-bearing reads (`get_secret*`) are carved out of the read-verb ALLOW and gated
  ahead of it (A08).
- `resolve_incident` on critical severity in production moves behind REQUIRE_APPROVAL (A12).
- Public comments that include internal hostnames are screened by a deterministic check
  before posting (A15).
- A merge that touches policy, workflows, entrypoints or lockfiles gets a sensitive-path
  operation like `merge:agent-definition` (D02, D03, D07, D11).
- Merge without CI or review evidence stays a deterministic evidence gate (C02, C03, C04).

## Reuse for .github#151 and #310

Treat `tests/fixtures/semantic_decision/v1/` and the `report` command as an offline
evaluation corpus: any classifier, model or rule set can be run against the same
fixtures and compared on the dangerous-false-negative line.

## Follow-up issue body: live run (to be filed only if the owner wants the data)

> **Spike 556 live run: Jev vs Clef-flash measurements**
>
> Run `tools/semantic_decision_spike.py run --allow-live` for `typesafe-jev` and
> `cloudflare-clef-flash` (and `cloudflare-clef` only if Clef-flash quality is poor) over
> the 45 fixtures, 5 repeats, then `report`.
>
> Preconditions, all required before starting: an account owner for Cloudflare Workers AI
> and for TypeSafe; credentials issued by them and read from env only; a hard spend cap
> (the estimate is about 450 calls per provider, well under $0.05 at the quoted list prices,
> to be re-checked); a **named approver** for that spend.
>
> Location: a one-off pod in the `admins` tenant namespace, the same network path as
> `mctl-agents-worker`; state the source location in the results. A run from a workstation
> is a recorded deviation.
>
> Re-record at run time: model IDs (Jev via `GET /v1/models`), API versions, request
> schema, prices, context limits. Verify the assumed request schemas against
> `https://api.typesafe.ai/redoc` and the Cloudflare docs first; a provider that does not
> exist as described is a NO_GO input for that provider.
>
> Output: updated `docs/spikes/556-semantic-decision-models.md` with the live table, the
> dangerous false negatives on their own line, and one of ADOPT_JEV, ADOPT_CLEF_FLASH,
> LIMITED_GO, NO_GO. Confirm the 300 ms p95 budget with the owner first. No production
> wiring in that issue.
