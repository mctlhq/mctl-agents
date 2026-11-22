# Subscription-backed inference for MCTL Agents — research evidence snapshot

> Status: **RESEARCH / PRIMARY POLICY SOURCES REVERIFIED / INDEPENDENT REVIEW ON REVISED HEAD PENDING**. Issue: [mctl-agents#1110](https://github.com/mctlhq/mctl-agents/issues/1110). Evidence accessed **2026-11-22**. Prepared as a documentary evidence snapshot; **not an executed Investigator run**, not an OpenSpec proposal, and not an implementation authorization.

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
| Google official antigravity-acp (G1; G3) | **Permitted only as a directly prompted, single-user interactive editor/IDE ACP** using Google-signed unmodified binary/native oauth-personal; **NOT permitted when MCTL/Codex/Claude delegates to ACP** | **UNKNOWN** without deployment-specific permission | **NO-GO under consumer ACP terms absent new published permission** | Plan/ACP limits; MCTL usage observability unverified |
| Google OAuth token reuse, MCTL-orchestrated ACP delegation, or headless wrapper (G1; G3) | **PROHIBITED** on consumer plans | **PROHIBITED** | **PROHIBITED** | No experiment |
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

- **P1**: treating official Google ACP support for interactive editors as a license to delegate from MCTL/Codex/Claude agents (even human-approved), or to use custom OAuth/headless wrapping. The 2026-11-16 Google response (G3) explicitly blocks both. Stop all such implementation.
- **P1**: carrying a single owner subscription into shared hosted tenant work or assuming commercial SIWC access from successful local dogfood. Block absent provider permission.
- **P1**: misrepresenting estimated API list price as subscription invoice or silently consuming overage credits.
- **P2**: confusing Hermes Nous Portal billing with per-vendor subscription quota.
- **P2**: treating Google Dec 2 rumor as official release or using unmodified ACP without proving single-user-interactive deployment constraints.
- **P2**: pretending current MCTL Anthropic-Messages provider seam directly speaks SIWC Responses.
- **P2**: assuming that OpenAI self-hosted VMs support per-host grant revocation or per-host quota accounting; O6 explicitly says they do not yet.
- **P2**: conflating successful Codex app-server authentication with MCTL capability/approval compatibility.

## Evidence gaps, dates, decision

- Not measured: live SIWC grant for a normal MCTL non-admin, quota/usage, Codex app-server SIWC behavior, direct Responses tool loop, hosting permissions beyond docs, Claude Ultra account actual capacity, deployment-specific Google ACP authorization, any new Google Dec 2 official API.
- Check Google on **2026-12-02** using Google's own published docs/ToS/SDK: supported harness, OAuth client and registration, eligible plans, region, quota semantics, host model, refresh/revocation, production/commercial constraints. If no evidence: **GOOGLE_PENDING_NO_GO**. No claim of launch.
- Provisional policy verdict: **continue #891 as the sole OpenAI SIWC research path; retain API/BYOK as the shared hosted baseline; do not implement direct Google/Claude subscription OAuth; wait for independent review.** No OpenSpec / implementer GO is implied.

## Review acceptance

An independent reviewer must check **each** provider-policy conclusion against the linked source, verify MCTL code/commit anchors, distinguish data from unmeasured assumptions, list P1/P2/P3 with remediation, and give **PASS / CHANGES_REQUESTED / NEEDS_HUMAN / UNOBSERVABLE**. A self-review is **not** an independent review. A reviewed research report is not approval to generate OpenSpec. Refs #1110; no Closes directive.

## Primary-provider policy re-verification — 2026-11-22 (second pass)

**Purpose:** Close the independent reviewers' stated `UNOBSERVABLE` external-source gap for the *research report*; this is source verification by the researcher, **not** an independently executed reviewer or legal clearance from any provider. Sources are live documentation/forum pages with access date 2026-11-22. They are not immutable snapshots; revalidate before integration/rollout. Individual source assertions below are narrowly scoped.

### OpenAI SIWC / Codex: verified documentation (O1–O6, plus O7–O9)

- [O1: Overview](https://developers.openai.com/siwc/token-sharing-open-source) — locally hosted/open-source apps may request user consent for plan-backed Responses; **paid or remotely hosted** apps are directed to an [interest form](https://developers.openai.com/siwc/token-sharing-open-source). A normal end-user's on-prem laptop and their **own** self-hosted VM may share one issued client/workspace, but must each have their own stable opaque host ID. **Not permission** for shared MCTL cloud workloads.
- [O7: Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in) and [O8: official integration example](https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt) — `openid email profile offline_access resource.invoke chatgpt.tokens.use.direct` requested; identity sign-in alone is not inference entitlement. Validate the issued client ID, ID-token identity, and actually granted `chatgpt.tokens.use.direct` before any inference. `dynamic_agent_client` is a registration entry point, not the token refresh client ID.
- [O3: Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference) — account-specific model discovery and public `POST /v1/responses`; not ChatGPT `backend-api` or direct access to existing ChatGPT web conversations. Discovery is not proof of entitlement for a specific inference; only completed request proves that invocation.
- [O4: Preview limits](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) — `store:false`, `stream:true`, client-managed history, no `previous_response_id` for HTTP; no hosted MCP/connectors, Code Interpreter, file search, image generation or native computer use. Local Codex tools are a **distinct** execution authority and must pass MCTL capability gates. No implied ChatGPT conversation wake-up.
- [O5: Codex app-server](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server) — app-owned OAuth token passed to a controlled child process through `ACCESS_TOKEN`; stdout RPC, explicit token refresh and restart, local `thread/resume`. Codex `model/list` may be bundled/cached; don't treat this as fresh account entitlement.
- [O2: Accounts/sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions) — distinct client/token registrations, refresh serialization, explicit disconnect/revoke failure UX; per-app usage limits and account-level usage display. ChatGPT Plus 5-hour allowance shared among apps and not a dedicated MCTL allowance (Pro does not have this particular five-hour limit; other limits may still apply).
- [O6: Self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms) — secure transfer of *this user's* protected credentials to their own VM is documented; **host-specific usage attribution and revocation for transferred sessions are not yet available**. Model credential ownership, reauth, lease/refresh and full disconnect accordingly. Do not assume host-isolated revocation.
- [O9: OpenAI supported countries](https://help.openai.com/en/articles/7947663-chatgpt-and-openai-api-supported-countries-and-territories) — Montenegro listed for ChatGPT/API generally; SIWC grant still account/plan/workspace-specific and untested.

**OpenAI decision:** `POLICY_GO_LOCAL_RESEARCH_ONLY` for #891 user-controlled open-source CLI/Codex and documented same-user VM; **no live OAuth test under #1110**. `HOSTED_MULTI_TENANT_REQUIRES_PROVIDER_ACCESS` and must stay API/BYOK until an explicit grant. **No separate quota** or free inference claim.

### Google Gemini / Antigravity: verified terms; critical delegation restriction (G1–G4)

- [G1: Google staff 2026-11-02 clarification](https://discuss.ai.google.dev/t/conflicting-guidance-on-using-t3-code-with-personal-google-ai-subscriptions/197131) — unmodified Google-signed `antigravity-acp` with native `oauth-personal` inside an ACP-compatible interactive editor/client, direct user prompts, Gemini models in plan: permitted. Credential extraction, proxy, headless wrapper and account rotation: prohibited on consumer plans.
- [G3: Google staff 2026-11-16 clarification](https://discuss.ai.google.dev/t/tos-clarification-is-human-supervised-codex-to-antigravity-acp-delegation-permitted/200697) — **even original `agy acp` delegated to by Codex/Claude or another external coding agent is prohibited under consumer plans, including with a human approving each task**. This removes ambiguity left by G1: interactive client permission is **not** permission for MCTL's agent-to-agent orchestration.
- [G4: Google Antigravity Additional Terms §6](https://antigravity.google/terms) and [official FAQ](https://antigravity.google/docs/faq) — consumer OAuth cannot be reused in third-party agent software; use Gemini API-key / Gemini Enterprise / Vertex instead. Headless `agy -p` direct scripting is distinct from using it as an external agent's delegated worker.
- [G4 FAQ regional availability](https://antigravity.google/docs/faq) — Montenegro is in the Antigravity geographical availability list **but also in the list of regions without Google One AI plans**. Region/product eligibility are not equivalent; no claim that a Montenegro user can purchase/use Google AI Pro/Ultra without an account-specific check.
- [G2: Gemini consumer Code Assist deprecation](https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals) — consumer Gemini CLI IDE sign-in deprecated from 2026-06-18; not a fallback consumer-plan auth entitlement for MCTL.
- [N1: TestingCatalog 2026-11-22](https://www.testingcatalog.com/gemini-subscriptions-may-reach-hermes-agent-on-december-2/) — Dec 2 remains a **third-party projection**, not a signed Google exception. **Checkpoint 2026-12-02** must capture any new official policy/integration docs before changing `GOOGLE_SUBSCRIPTION_NO_GO_FOR_MCTL_ORCHESTRATION`.

**Google decision:** `GO_API_BYOK_RESEARCH` only. `NO_GO` consumer Antigravity OAuth as a model provider for MCTL DevLoop, even via unmodified ACP and human-in-loop delegation. Independently interactive editor scenario may be allowed but is not the MCTL agent integration target.

### Anthropic Claude: verify both exception and restriction (A1–A2)

- [A1: Claude Code legal/compliance](https://code.claude.com/docs/en/legal-and-compliance) — a vendor can preinstall **unmodified** Claude Code under applicable Commercial Terms (absent different mutual terms), each end-user using their own credentials and paying directly. This differs from a developer embedding the Agent SDK and offering arbitrary Claude.ai OAuth or routing tenant work onto somebody else's Pro/Max/Ultra plan. Third-party developers must use API or supported clouds; do not collect/intermediate Claude.ai tokens.
- [A2: Anthropic 'Log in to your Claude account'](https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account) — Anthropic **may discretionarily allow** subscribers with usage credits to use certain third-party tools, with charges taken from **usage credits**, not necessarily included subscription limits. It is not a general developer-granted consumer OAuth integration.
- [H1: Hermes providers](https://hermes-agent.nousresearch.com/docs/integrations/providers) — vendor's Claude Max OAuth describes purchased extra usage credits only, baseline Max allowance untouched; Hermes Claude Pro OAuth unavailable. Hermes technical implementation does **not** establish policy permission for MCTL. Keep Claude API / Bedrock / Vertex / eligible linked billing as the MCTL third-party product routes.

**Anthropic decision:** `API_OR_LINKED_BILLING_ONLY_FOR_MCTL_AGENT_SDK`. Separate unchanged Claude Code deployment requires own product/terms/identity analysis; **NO GO** for shared consumer OAuth inference.

### Hermes Nous: split billing and auth precisely (H1)

- [H1: Nous provider table](https://hermes-agent.nousresearch.com/docs/integrations/providers) explicitly distinguishes: (a) *Nous Portal* subscription charges with Nous; (b) Codex ChatGPT OAuth (Hermes-specific plan quota **not documented**); (c) Claude Max OAuth spending *extra usage credits* rather than base plan allowance; (d) Gemini consumer plan **no documented Hermes OAuth route**, while API keys / Vertex use separate API/project quota and billing.
- Hermes supports importing existing `~/.codex/auth.json`, but that vendor feature **is not permission** for MCTL to import someone else's Codex token. #891 should use MCTL's own documented SIWC grant and stay in the same user's controlled host; any external account-file shortcut is out of scope.

### Decision gates and source reliability

| Gate | Status as of 2026-11-22 | Satisfying future evidence |
|---|---|---|
| Research documentation primary-source policy verification | **PASS (researcher verified URLs); independent revised-HEAD review pending** | Reviewer independently fetches source claims + CI |
| MCTL local personal BYOS design | **CONDITIONAL RESEARCH GO** via #891 | Normal non-admin UX, authentic consent, offline fixtures and safe credentials, terms and quotas |
| Local SIWC live pilot | **NO GO under #1110** | New owner live-test approval, eligibility, spend and account isolation |
| Hosted multi-tenant subscription inference | **NO GO** | Provider written commercial access + governance, tenant isolation, quota/legal checks |
| Google consumer OAuth inside MCTL agent loop | **NO GO** | New Google explicit exception for orchestration (not press prediction) |
| Claude consumer OAuth inside custom MCTL Agent SDK | **NO GO** | Anthropic permission specifically covering target product pattern |
| Docs-only governed merge | **PENDING NEW-HEAD INDEPENDENT REVIEWS AND CI, OWNER MERGE GO** | No P1/P2 unresolved, source verification accepted; document merge cannot authorize implementation |
| OpenSpec, implementer, production | **NO GO** | Separate owner authorization after research verdict and optional isolated pilot |

Review findings for original snapshot: *provider policy assumptions were not verified by Claude* (scope `UNOBSERVABLE`). This second pass directly checked official provider sources and found **new actionable material corrections**: the Google 16-Nov delegation prohibition, OpenAI VM revocation limitation, and geographic Google plan availability. Those were integrated into the revised report. The changed HEAD must be independently re-reviewed; earlier Agy/Claude PASS applies only to `cfe4f0b`.
