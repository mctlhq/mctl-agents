# Subscription-backed inference for MCTL Agents — research evidence snapshot

> Status: **RESEARCH / DRAFT / INDEPENDENT REVIEW PENDING**. Issue: [mctl-agents#1110](https://github.com/mctlhq/mctl-agents/issues/1110). Evidence accessed **2026-11-22**. Prepared as a documentary evidence snapshot; **not an executed Investigator run**, not an OpenSpec proposal, and not an implementation authorization.

## Research question and hard boundary

Can a normal user attach their own AI subscription to a user-controlled MCTL agent without granting the shared MCTL backend use of that plan? Answer provider by provider, deployment by deployment. A technically working OAuth exchange is *not* evidence of terms-compliant use or extra subscription capacity. No live OAuth, paid inference, code change, worker trigger, runtime change, credential migration or automatic proposal generation was performed for this snapshot.

## Source register (primary unless identified otherwise)

| ID | Source / source-pinned artifact | Evidence level and observation |
|---|---|---|
| O1 | [OpenAI SIWC overview](https://developers.openai.com/siwc/token-sharing-open-source) | Official docs, viewed 2026-11-22. ChatGPT plan usage is for open-source/locally hosted apps; paid or remotely hosted app is directed to an interest form. Distinct OAuth client and host IDs. |
| O2 | [OpenAI accounts/sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions) | Official, viewed 2026-11-22. Local credential storage, refresh serialization, revocation guidance, Plus five-hour usage shared between apps; no separate app allowance. |
| O3 | [OpenAI SIWC models/inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference) | Official, viewed 2026-11-22. Account-specific model catalog and bearer-auth public /v1/responses. |
| O4 | [OpenAI preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) | Official, viewed 2026-11-22. HTTP requires store:false, stream:true, client-supplied history; hosted tools and persistent Responses history unavailable. |
| O5 | [OpenAI Codex app-server SIWC](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server) | Official, viewed 2026-11-22. Credential passed in child environment, stdio RPC, app-owned refresh/restart and thread/resume. |
| O6 | [OpenAI self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms) | Official, viewed 2026-11-22. Same user's self-hosted host is documented, with distinct host identifiers and protected credential transfer. |
| A1 | [Anthropic Claude Code legal guidance](https://code.claude.com/docs/en/legal-and-compliance) | Official, viewed 2026-11-22. Third-party product/Agent SDK developers should use API keys / supported cloud providers, not offer consumer Claude.ai login or route third-party requests via consumer plans. Separate case: unmodified Claude Code preinstalled in a product under commercial terms, each end user signs into their own credentials. |
| G1 | [Google Antigravity official forum reply (2026-11-02)](https://discuss.ai.google.dev/t/conflicting-guidance-on-using-t3-code-with-personal-google-ai-subscriptions/197131) | Google-staff policy interpretation, viewed 2026-11-22. Unmodified, Google-signed antigravity-acp with native oauth-personal in ACP client is permitted for single-user interactive Gemini work. Third-party token reuse, headless agy -p wrappers and account rotation prohibited on consumer plans. |
| G2 | [Google consumer Code Assist deprecation](https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals) | Official, last updated 2026-10-09. Gemini CLI / IDE consumer-plan 'Login with Google' path deprecated June 18, 2026; does not imply access to Gemini consumer plan from MCTL. |
| H1 | [Nous Research Hermes providers docs](https://hermes-agent.nousresearch.com/docs/integrations/providers) | Vendor implementation docs, viewed 2026-11-22. Nous Portal subscription is Nous-billed; Hermes Codex uses ChatGPT OAuth; Claude Max OAuth path described as extra-usage credits; Gemini consumer subscription not supported through Hermes; Gemini API key / Vertex are separate billing. Claims are evidence about Hermes, not permission granted to MCTL. |
| N1 | [TestingCatalog report dated 2026-11-22](https://www.testingcatalog.com/gemini-subscriptions-may-reach-hermes-agent-on-december-2/) | Third-party report/forward-looking signal, **not a Google release or integration permission**. 2026-12-02 is a checkpoint, not confirmed universal rollout. |
| M1 | [MCTL model-policy providers at 05c1dc83](https://github.com/mctlhq/mctl-agents/blob/05c1dc83b6c025aa5128e6d9234023eb513f95a0/docs/model-policy-providers.md) | Source-pinned. Provider seam supports **anthropic-messages only**, not arbitrary Responses/ACP; validation is fail-closed and no committed alternate profile enabled. |
| M2 | [MCTL ADR-012 at 05c1dc83](https://github.com/mctlhq/mctl-agents/blob/05c1dc83b6c025aa5128e6d9234023eb513f95a0/docs/adr/012-model-usage-cost-attribution-contract.md) | Source-pinned contract. Calculated SDK/list-price estimate is not actual subscription invoice; usage grain is run x model. |
| M3 | [MCTL Claude #917 report at 05c1dc83](https://github.com/mctlhq/mctl-agents/blob/05c1dc83b6c025aa5128e6d9234023eb513f95a0/docs/spikes/917-claude-ultra-capacity.md) | Source-pinned. PENDING_EVIDENCE; account tier and actual quota/spend not observed. |
| M4 | [Existing Go CLI auth at ad59da19](https://github.com/mctlhq/mctl-gitops/blob/ad59da1964ed239b336c96ba527423c32b89486d/cli/mctl/cmd/auth.go) | Source-pinned MCTL CLI location; #891 is owner of SIWC feasibility and detailed auth/UX. |
| M5 | [Existing Codex/Claude bridge at 34369cf9](https://github.com/mctlhq/mctl-claude-remote/blob/34369cf9873745a3b4d5c63ac851d976b81f428d/docs/chatgpt-orchestrator-bridge.md) | Source-pinned design with Gate 1 (Codex app-server) and Gate 2 (slot addressing) observed on 2026-11-03. Not evidence that new SIWC integration is implemented. |

## Subscription / identity / execution matrix

| Provider and access path | Local single-user | Same user's self-hosted VM | Shared hosted MCTL tenants | Quota & cost source |
|---|---|---|---|---|
| OpenAI SIWC direct Responses (O1-O6) | **Documented for eligible open-source clients**, subject to actual OAuth grant | **Documented** with same user/client and distinct host ID; live proof not performed | **Requires separate OpenAI commercial/remote-access approval**, not assumed from local grant | User's ChatGPT plan and app settings; Plus shared five-hour window; token estimate != provider bill |
| OpenAI SIWC → local Codex app-server (O5) | **Documented** adapter mechanism, MCTL-specific permissions untested | **Conditional**, same user and correct host/token lifecycle | **Not authorized by local instructions** | Same subscription consumption as SIWC, not an independent quota |
| Google official antigravity-acp (G1) | **Permitted for single-user interactive development** using Google-signed unmodified binary/native oauth-personal, Gemini entitlement only | **UNKNOWN** without deployment-specific permission | **NO-GO under consumer ACP terms absent new published permission** | Plan/ACP limits; MCTL usage observability unverified |
| Google OAuth token reuse / headless wrapper (G1) | **PROHIBITED** on consumer plans | **PROHIBITED** | **PROHIBITED** | No experiment |
| Google Gemini API key / Vertex (G1/G2/H1) | **Documented API path** | **Documented API path** | **Candidate conventional API path**, apply service terms | API/Cloud project billing or quotas, not consumer subscription |
| Claude Code unmodified binary (A1) | **Conditional** per end-user auth, product terms | **Conditional**, product terms | **Specific commercial terms + end-user own auth**; no cross-user subsidy | User's agreement; no pooled plans |
| Anthropic Agent SDK / custom third-party Claude OAuth (A1) | **No official consumer Claude.ai sign-in path for MCTL** | **No** | **No** | Use API/Bedrock/Vertex supported billing |
| Hermes Nous Portal (H1) | Nous subscription, **not users' Google/Claude/OpenAI plans** | Depends on Nous service terms, not established | Unknown | Nous-billed quota/plan; separate commercial contract |
| Hermes Codex OAuth (H1/O1) | Vendor feature documented; precise MCTL authorization and quota semantics **not proven by Hermes feature** | Unknown for Hermes | No inference of commercial grant | Plan-specific; unknown; test under #891 if authorized |

## Quota / FinOps contract (hypothesis; no unmeasured zeroes)

- Minimum record: provider, auth_mode, user-controlled vs service-owned credential scope, runtime_owner, consumer_scope, billing_mode, model, quota_window_kind, remaining, reset_at, shared_plan_group, app_limit, credit_spend_opt_in, estimated_list_cost, provider_reported_actual_charge, observed_at, source and confidence.
- Separate four monetary notions: (1) recurring subscription fee (not attributable per invocation without chosen allocation); (2) included subscription quota; (3) optional extra usage credits actually billed (provider billing evidence required); (4) API key pay-per-use charges. Do not label an SDK's calculated_cost as actual spend (M2).
- Failure classes must not collapse: token expired/revoked, org disallows subscription, user not entitled, app-specific limit, weekly/rolling rate limit, paid-credit exhaustion, 429 retryable throttle, capacity/provider outage. Record stable low-cardinality reason; redact account IDs and tokens.
- Unknown fields stay NULL/UNKNOWN. Do not turn 'No usage evidence' into zero consumption; no automatic paid-credit fallback or cross-account rotation.

## Existing MCTL ownership and necessary safety gates

- **#891** is the canonical detailed SIWC/BYOS end-user CLI and Codex app-server research. Do not create a second SIWC implementation or an owner-specific credential path.
- **#824 / M1** provides an Anthropic-Messages transport seam; SIWC Responses and Google's ACP are different protocols and require explicit compatibility analysis, not a base URL override.
- **#917 / M3** owns account-capacity and paid-extra-credits semantics; account tier / provider billing remains unobserved, so no throughput/savings claim.
- **#746** owns the thin local adapter decision; reuse only if a measured local lifecycle requirement is found.
- **#1100/#1101** own DevLoop attribution/observe-only efficiency. Neither should be converted into paid-plan capacity enforcement without a separate decision.
- **#641/#364** document prior organization-OAuth and quota-exhausted misclassification; review their current status before assuming robust fail-closed UX.
- Execution must keep MCTL approval, capability allowlists, run token, tenant binding, audit and revocation fencing authoritative even if Codex/Claude tools are technically callable.

## Bounded research verification (offline only under this GO_RESEARCH)

1. Synthetic OAuth issuer: PKCE+state+nonce, dynamic client ID persistence, refresh rotation under concurrent process lock, expire/revoke and logout. No real credentials or network calls.
2. Fake Responses/Codex adapter: same review fixture, tool call which MCTL denies, resumed multi-turn history; check stream-complete vs interrupted and correct handling of store:false/stream:true.
3. Fake quota provider: shared rolling-window exhaustion, per-app restriction, optional credits disabled, plan ineligible, provider down. Assert no silent expensive or cross-user fallback.
4. Negative tenant fixture: identity A credential cannot serve tenant B, even if the same machine or service has the secret; no token present in logs/metrics/CI/artifacts.
5. Compare existing CLI + direct Responses vs Codex app-server vs conventional API-key baseline; measure outcomes, effort, subprocess/RSS overhead, tool-policy enforcement and credential exposure. **Tests are only proposed, not executed**.

## P1/P2 risk candidates for independent reviewer

- **P1**: turning official Google ACP exemption into permission for custom MCTL OAuth/headless wrapper; provider policy contradicts that. Block any such implementation.
- **P1**: carrying a single owner subscription into shared hosted tenant work or assuming commercial SIWC access from successful local dogfood. Block absent provider permission.
- **P1**: misrepresenting estimated API list price as subscription invoice or silently consuming overage credits.
- **P2**: confusing Hermes Nous Portal billing with per-vendor subscription quota.
- **P2**: treating Google Dec 2 rumor as official release or using unmodified ACP without proving single-user-interactive deployment constraints.
- **P2**: pretending current MCTL Anthropic-Messages provider seam directly speaks SIWC Responses.
- **P2**: conflating successful Codex app-server authentication with MCTL capability/approval compatibility.

## Evidence gaps, dates, decision

- Not measured: live SIWC grant for a normal MCTL non-admin, quota/usage, Codex app-server SIWC behavior, direct Responses tool loop, hosting permissions beyond docs, Claude Ultra account actual capacity, deployment-specific Google ACP authorization, any new Google Dec 2 official API.
- Check Google on **2026-12-02** using Google's own published docs/ToS/SDK: supported harness, OAuth client and registration, eligible plans, region, quota semantics, host model, refresh/revocation, production/commercial constraints. If no evidence: **GOOGLE_PENDING_NO_GO**. No claim of launch.
- Provisional policy verdict: **continue #891 as the sole OpenAI SIWC research path; retain API/BYOK as the shared hosted baseline; do not implement direct Google/Claude subscription OAuth; wait for independent review.** No OpenSpec / implementer GO is implied.

## Review acceptance

An independent reviewer must check **each** provider-policy conclusion against the linked source, verify MCTL code/commit anchors, distinguish data from unmeasured assumptions, list P1/P2/P3 with remediation, and give **PASS / CHANGES_REQUESTED / NEEDS_HUMAN / UNOBSERVABLE**. A self-review is **not** an independent review. A reviewed research report is not approval to generate OpenSpec. Refs #1110; no Closes directive.