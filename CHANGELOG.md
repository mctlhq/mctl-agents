# Changelog

## [1.26.0](https://github.com/mctlhq/mctl-agents/compare/1.25.0...1.26.0) (2026-08-06)


### Features

* **auth:** add CLAUDE_CODE_OAUTH_TOKEN_SECONDARY and ANTHROPIC_API_KEY_SECONDARY fallback ([79ee36e](https://github.com/mctlhq/mctl-agents/commit/79ee36edaada4d892609e320069423854430dcf3))
* **auth:** add CLAUDE_CODE_OAUTH_TOKEN_SECONDARY and ANTHROPIC_API_KEY_SECONDARY fallback support ([04e6c31](https://github.com/mctlhq/mctl-agents/commit/04e6c31a91b17216a269663d3d88fedf2bc3d8e2))


### Bug Fixes

* **config:** switch service_agent model mapping to cheap (claude-haiku-4-5) ([bdcb164](https://github.com/mctlhq/mctl-agents/commit/bdcb16454676b6ed80696d30b8114c7c205b8b96))
* **config:** switch service_agent model mapping to cheap (claude-haiku-4-5) ([296124b](https://github.com/mctlhq/mctl-agents/commit/296124bb878720cf9fe00b2575a0c30c9aa4891a))

## [1.25.0](https://github.com/mctlhq/mctl-agents/compare/1.24.0...1.25.0) (2026-08-06)


### Features

* **temporal:** add IncidentLoopWorkflow and 30-minute schedule ([a399fb5](https://github.com/mctlhq/mctl-agents/commit/a399fb55aa17231345b0fdf20cb040288d2b9525))
* **temporal:** add IncidentLoopWorkflow and 30-minute schedule ([dd81a18](https://github.com/mctlhq/mctl-agents/commit/dd81a18b54d536e74da0673d4738e110fc3e4056))
* **temporal:** add IssuePollWorkflow and 30-minute schedule ([9d7a084](https://github.com/mctlhq/mctl-agents/commit/9d7a084bb5088f2f7e2b54f8875ef7567975a850))
* **temporal:** add IssuePollWorkflow and 30-minute schedule ([45baf36](https://github.com/mctlhq/mctl-agents/commit/45baf365e08fa4bfc009d29c6cebcb9a14ecc776))
* **temporal:** add ReconcileWorkflow and schedule ([e6ed8dc](https://github.com/mctlhq/mctl-agents/commit/e6ed8dc900c6da73fc4c726a2fa8b1bd50289cb5))
* **temporal:** add ReconcileWorkflow and schedule ([25e411d](https://github.com/mctlhq/mctl-agents/commit/25e411da884575690fde93c40e9685093953230a))
* **temporal:** refine discovery and orphan activities with async thread execution ([cb953e4](https://github.com/mctlhq/mctl-agents/commit/cb953e4363164f2df0b18e7f5770e4940ab69a1c))
* **temporal:** refine discovery and orphan activities with async thread execution and active workflow filtering ([2283fb2](https://github.com/mctlhq/mctl-agents/commit/2283fb24363c0877deb4da8b53a5420f67406265))


### Bug Fixes

* **temporal:** use ScheduleIntervalSpec for Temporal Python SDK schedule interval ([7a28f93](https://github.com/mctlhq/mctl-agents/commit/7a28f93eb4e8ed1ee748ba645f0199b4cac28f14))

## [1.24.0](https://github.com/mctlhq/mctl-agents/compare/1.23.0...1.24.0) (2026-08-06)


### Features

* register mctl-academy as an issue-driven service ([4f334c7](https://github.com/mctlhq/mctl-agents/commit/4f334c74f6b63859ef9183c2351ddba28fa814d7))
* register mctl-academy as an issue-driven service ([0dcbeba](https://github.com/mctlhq/mctl-agents/commit/0dcbeba4f76bf67147631108879874b4c74d32dc))


### Bug Fixes

* **review:** apply the already-tagged/digested guard before the digest branch too ([00a5103](https://github.com/mctlhq/mctl-agents/commit/00a510382aa2b0baaa51d5c4817f30b6e4501ed5))
* **temporal:** don't double-tag an image_repository that already has one ([9466565](https://github.com/mctlhq/mctl-agents/commit/9466565bdc10a401f37beccdfba46fc7540adbd9))
* **temporal:** don't double-tag an image_repository that already has one ([c7efaef](https://github.com/mctlhq/mctl-agents/commit/c7efaef7cf262c8c2a0c609b3ffeac18ea616cc9))

## [1.23.0](https://github.com/mctlhq/mctl-agents/compare/1.22.0...1.23.0) (2026-08-05)


### Features

* **temporal:** issue-poller dispatches DevLoopWorkflow instead of investigating in-process ([61b5d2f](https://github.com/mctlhq/mctl-agents/commit/61b5d2f2a7a9da66439c36f0fe484ea2ef83d235))


### Bug Fixes

* catch a broken git-log range instead of masking it as no-releasable-commits ([321b5a0](https://github.com/mctlhq/mctl-agents/commit/321b5a0e03a7bf410ffd4949d7ed683c4ca491ee))
* **ci:** catch commits release-please would silently skip ([c74a94b](https://github.com/mctlhq/mctl-agents/commit/c74a94b9f01a8faf3b605f1b269a9a78d8fe7c63))
* **ci:** catch commits release-please would silently skip ([4e5e64f](https://github.com/mctlhq/mctl-agents/commit/4e5e64ff2b447c71e7182ace176b7f5e847643d0))
* repair the workflow file broken by the previous commit ([c17d749](https://github.com/mctlhq/mctl-agents/commit/c17d749f819274359b67630b524bc648dad6dfcb))

## [1.22.0](https://github.com/mctlhq/mctl-agents/compare/1.21.0...1.22.0) (2026-08-05)


### Features

* **agents:** add AgentManifest contract + validator (phase 1) ([f436b6c](https://github.com/mctlhq/mctl-agents/commit/f436b6c5fcc554024df7c47982f99fab08515c35))
* **agents:** add AgentManifest contract + validator (phase 1) ([cef7e7b](https://github.com/mctlhq/mctl-agents/commit/cef7e7b27e75832b3302436e9a3a811a812cf323))
* **temporal:** DevLoopWorkflow — issue -&gt; investigate -&gt; approve -&gt; implement ([7b4479b](https://github.com/mctlhq/mctl-agents/commit/7b4479b2248e5bc0c0ff5cc83c601a692c7c1b5b))
* **temporal:** DevLoopWorkflow — issue -&gt; investigate -&gt; approve -&gt; implement ([24b586a](https://github.com/mctlhq/mctl-agents/commit/24b586a8fe1379845fb0e7bd5e6bc8b49561521f))


### Bug Fixes

* **agents:** address Codex round-4 P2s posted after Claude's approval ([537f8f5](https://github.com/mctlhq/mctl-agents/commit/537f8f539a6431e97d62e68310ef00dc191dfd25))
* **agents:** address remaining Codex P2s from round-2 review ([875b9cd](https://github.com/mctlhq/mctl-agents/commit/875b9cd89e80a7560ff49fae8ddb84db57b76cb1))
* **agents:** address review findings on the manifest validator ([9bc814c](https://github.com/mctlhq/mctl-agents/commit/9bc814cda02cefddc3086d069989ecd9ec5e8f96))
* **agents:** restore orchestrator.options after clean-env comparison ([4fb2cff](https://github.com/mctlhq/mctl-agents/commit/4fb2cff00ab2ae303f9913030f66aaadebbd84d0))
* bound retry policies and poll resilience per review (PR [#105](https://github.com/mctlhq/mctl-agents/issues/105)) ([82cf0ae](https://github.com/mctlhq/mctl-agents/commit/82cf0ae267f356e631c7913dc5493e0e4a30a3db))
* close remaining duplicate-submit gap, verify gitops scoping dependency (PR [#105](https://github.com/mctlhq/mctl-agents/issues/105) review round 3) ([9575ae3](https://github.com/mctlhq/mctl-agents/commit/9575ae3e0029c61b5bce55cbcee00571a8e8981e))
* **diag:** request explicit permissions, avoid run: interpolation ([1a0d01d](https://github.com/mctlhq/mctl-agents/commit/1a0d01d89c60531083190802650af6564034a2e9))
* resumable Argo polling, scope implement to issue's repo, fix audit trail (PR [#105](https://github.com/mctlhq/mctl-agents/issues/105) review) ([ecda152](https://github.com/mctlhq/mctl-agents/commit/ecda1529914cc4e39386dc13ddbef7e7e8fe2ef1))

## [1.21.0](https://github.com/mctlhq/mctl-agents/compare/1.20.2...1.21.0) (2026-08-02)


### Features

* **agents:** e3649b04 ([b5b01c1](https://github.com/mctlhq/mctl-agents/commit/b5b01c1abddd1916e21b29547f9e9c1e313a51d8))
* **auth:** refresh GITHUB_TOKEN from a mounted file before each gh/git call ([ba6abd4](https://github.com/mctlhq/mctl-agents/commit/ba6abd443addecdcde29659e597d460940a87bab))
* **auth:** refresh GITHUB_TOKEN from a mounted file before each gh/git call ([7ce7369](https://github.com/mctlhq/mctl-agents/commit/7ce73698ceefb57bdd0389e32f5b43154538dcf7))


### Bug Fixes

* address Codex/claude[bot] review findings on PR [#83](https://github.com/mctlhq/mctl-agents/issues/83) ([979b084](https://github.com/mctlhq/mctl-agents/commit/979b084a6fba238ef664ac1d5f7b99b68cc76121))
* align skipped outcome totals ([9410081](https://github.com/mctlhq/mctl-agents/commit/941008189a3296916bb28b19d5ad897d7db03409))
* bound implementer model runtime ([2d20b45](https://github.com/mctlhq/mctl-agents/commit/2d20b4575b47dd14ca809a1618c6ec9831d5a196))
* bound implementer runtime operations ([658c6c7](https://github.com/mctlhq/mctl-agents/commit/658c6c79805bf3684167c0f84639897ae42f3041))
* catch UnicodeDecodeError when refreshing GITHUB_TOKEN from file ([3a26b71](https://github.com/mctlhq/mctl-agents/commit/3a26b71ea361f45f9de52e4ade74fc34200e33f5))
* close aborted progress spans ([bd0b07c](https://github.com/mctlhq/mctl-agents/commit/bd0b07cf3eb3e54a3f2d902312cef4c881fe03e4))
* enforce one-proposal implementer runs ([6116384](https://github.com/mctlhq/mctl-agents/commit/611638454b25b48b9b9754022616e8d188224fd7))
* enforce quota-safe implementer batching ([32ff931](https://github.com/mctlhq/mctl-agents/commit/32ff9316396095b332b864f87e15a432affa5528))
* **incident-responder:** stop silent false-green when mctl MCP never connects ([8768c22](https://github.com/mctlhq/mctl-agents/commit/8768c2266753914ebfdc2f6d0feef169e3758a90))
* **incident-responder:** stop silent false-green when mctl MCP never connects ([394fec7](https://github.com/mctlhq/mctl-agents/commit/394fec78e26ccbb2310a6d3d9da08c338484d78b))
* label implementer progress logs ([178d2af](https://github.com/mctlhq/mctl-agents/commit/178d2afd2f55d6001d196c1853150078393cb9a8))
* label implementer progress logs ([4bab8e5](https://github.com/mctlhq/mctl-agents/commit/4bab8e5387fbeb23b16a362445c08d94ba2ae6dc))
* move status response parsing inside the McpNotConnectedError guard ([10699b1](https://github.com/mctlhq/mctl-agents/commit/10699b1cd8438aa0a5cdcff5775bdfb0f3655de1))
* prefer explicit skipped outcomes ([d13c46a](https://github.com/mctlhq/mctl-agents/commit/d13c46a615c63e80ef4b0c1d876dfcebd71d41cf))
* refresh GITHUB_TOKEN before merge_pr()'s direct gh pr merge call ([570c5ee](https://github.com/mctlhq/mctl-agents/commit/570c5ee715772fd4c06c203aeca40829bcdd60a1))
* report truthful implementer outcomes ([c38c4b9](https://github.com/mctlhq/mctl-agents/commit/c38c4b996d37785e8d737995c6ac12b220c448a8))
* report truthful implementer outcomes ([ace18b3](https://github.com/mctlhq/mctl-agents/commit/ace18b362a89766564b5d0ddda2faaed58ee30a5))
* scope batch policy to implementation ([e246f92](https://github.com/mctlhq/mctl-agents/commit/e246f922444e18a4242284a2413996fa157ca4f1))

## [1.20.2](https://github.com/mctlhq/mctl-agents/compare/1.20.1...1.20.2) (2026-07-29)


### Bug Fixes

* accept changed mergeable heads ([93e6e35](https://github.com/mctlhq/mctl-agents/commit/93e6e3591b3ea97c1f7675a6dfe4644004423bf8))
* preserve confirmed conflict quarantine ([bb0554a](https://github.com/mctlhq/mctl-agents/commit/bb0554aa055ab4772165f0b5c698e8dbb178f845))
* preserve confirmed conflict quarantine ([669dad6](https://github.com/mctlhq/mctl-agents/commit/669dad677c72508da497790eb4a3dfc26b0179de))
* preserve terminal proposal decisions ([7f199cb](https://github.com/mctlhq/mctl-agents/commit/7f199cb37d9a3dc5e48fc3f35c2b8fffc1cd62cf))
* preserve terminal proposal decisions ([4f13474](https://github.com/mctlhq/mctl-agents/commit/4f134748ca9016cfcd4e7c2b57236633ccb713a8))

## [1.20.1](https://github.com/mctlhq/mctl-agents/compare/1.20.0...1.20.1) (2026-07-29)


### Bug Fixes

* reconcile absent result branches ([aeb2437](https://github.com/mctlhq/mctl-agents/commit/aeb2437fb4053cf10fe7270f83522bfaa83c1a60))
* reconcile absent result branches ([32ee445](https://github.com/mctlhq/mctl-agents/commit/32ee44573c85370151856c4c6ee3b27f3cabd738))

## [1.20.0](https://github.com/mctlhq/mctl-agents/compare/1.19.0...1.20.0) (2026-07-29)


### Features

* **agents:** b892d8c7 ([8b17a8f](https://github.com/mctlhq/mctl-agents/commit/8b17a8fd21630c005847f8447ef31470f101da0e))
* make implementer PR lifecycle GitHub-first ([4b84461](https://github.com/mctlhq/mctl-agents/commit/4b84461de84809de17d4ca9a2251caa1ca671a49))
* make implementer PR lifecycle GitHub-first ([af54c06](https://github.com/mctlhq/mctl-agents/commit/af54c06bdcb834ab2633e2f190a3b1c6ba44f14c))


### Bug Fixes

* **agents:** swallow incident-responder failures in run_all ([3bf9af1](https://github.com/mctlhq/mctl-agents/commit/3bf9af1be33e59d8661541e5e13dfff1f8ef2c17))
* fail batches for quarantined proposals ([d7ecf98](https://github.com/mctlhq/mctl-agents/commit/d7ecf98626bb55713ecc58fe63a9e6d3321285db))
* keep global auth failures out of proposal state ([f42163f](https://github.com/mctlhq/mctl-agents/commit/f42163f26ecf695ae9c9dfc70c37a71af7ff2471))
* preserve quarantine and dry-run safety ([7de4096](https://github.com/mctlhq/mctl-agents/commit/7de4096fdc00824e0720597bc6b046d8a71972ef))
* reconcile accepted proposals with existing PRs ([e6e3077](https://github.com/mctlhq/mctl-agents/commit/e6e3077c0f46df302588f9f8e1b244f07adaea17))
* reset review retry budget after material change ([0dc64eb](https://github.com/mctlhq/mctl-agents/commit/0dc64eb2ea138295458939c410bdb825a72a88d6))

## [1.19.0](https://github.com/mctlhq/mctl-agents/compare/1.18.0...1.19.0) (2026-07-24)


### Features

* **models:** add policy resolver ([0503f1d](https://github.com/mctlhq/mctl-agents/commit/0503f1de5ef796bb10b1a61e475c22fae376c81a))
* **models:** add task model profiles ([09f226a](https://github.com/mctlhq/mctl-agents/commit/09f226a81a4a1c7e40039977c38e3bf2d238a30e))
* **models:** add task-based model policy ([c9a0252](https://github.com/mctlhq/mctl-agents/commit/c9a025272abc658bf4d88073663933632b0380f2))
* **models:** route agent tasks through policy ([595cec8](https://github.com/mctlhq/mctl-agents/commit/595cec823d772f3404bf4df8b1cd344c6d0ff336))

## [1.18.0](https://github.com/mctlhq/mctl-agents/compare/1.17.0...1.18.0) (2026-07-23)


### Features

* **agents:** incident-argo-id-slug-collision ([e316c46](https://github.com/mctlhq/mctl-agents/commit/e316c46341b6fcc3b767a2035c09cee6fcd055d2))
* **issue-poll:** distinct exit code when every issue hits rate-limit exhaustion ([8af0cb0](https://github.com/mctlhq/mctl-agents/commit/8af0cb0bf02ed4cd3b4cb1a0d38b13436715f6f0))
* **issue-poll:** distinct exit code when every issue hits rate-limit exhaustion ([a05e264](https://github.com/mctlhq/mctl-agents/commit/a05e264c7decfd7bcc3d7a47bdfab3afca9189a1))


### Bug Fixes

* **agents:** make incident-responder proposal slug collision-resistant ([95f7cca](https://github.com/mctlhq/mctl-agents/commit/95f7ccaeb44730076c296877f00bfde51e63c1ad))
* **issue-poll:** compare rate-limited failures against attempts, not failures ([d3ce6b2](https://github.com/mctlhq/mctl-agents/commit/d3ce6b24e000a42b0c35116126cfb7481c8d13d2))

## [1.17.0](https://github.com/mctlhq/mctl-agents/compare/1.16.1...1.17.0) (2026-07-15)


### Features

* **agents:** incident-17840745 ([bc61dc7](https://github.com/mctlhq/mctl-agents/commit/bc61dc7dd5c5fb11725487e61ad77091952a18c3))


### Bug Fixes

* **agents:** raise incident-responder budget from $2 to $5 ([2c514d7](https://github.com/mctlhq/mctl-agents/commit/2c514d718280b9e8d322ab56a4fe6956c648ed12))
* **ci:** detect claude-review SDK failure the outcome field misses ([d1fda0b](https://github.com/mctlhq/mctl-agents/commit/d1fda0b36727862de784f9676b4054e992e4dbfd))
* **ci:** detect claude-review SDK failure the outcome field misses ([36a0a7a](https://github.com/mctlhq/mctl-agents/commit/36a0a7a485570051e8a99111927c810a2ea3a632))
