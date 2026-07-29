# Changelog

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
