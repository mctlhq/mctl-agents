# Changelog

## [1.31.0](https://github.com/mctlhq/mctl-agents/compare/1.30.0...1.31.0) (2026-08-29)


### Features

* **dev-loop:** merge detection — get_pr_state polling + pr result stage ([bf3e767](https://github.com/mctlhq/mctl-agents/commit/bf3e767c0e901c1cb303fc540bfe115cecdf749c))
* **dev-loop:** merge detection — get_pr_state polling + pr result stage ([362a741](https://github.com/mctlhq/mctl-agents/commit/362a7417146f5b46cfb13a879ea639d49f0c0565)), closes [#214](https://github.com/mctlhq/mctl-agents/issues/214)
* **dev-loop:** show the merge-watch outcome in the status command ([8dfe76a](https://github.com/mctlhq/mctl-agents/commit/8dfe76a81e5b1d11e1e14633424f74dca748497f))


### Bug Fixes

* **dev-loop:** harden merge-detection per review ([ae7afd5](https://github.com/mctlhq/mctl-agents/commit/ae7afd5cc9a644303e36d7d1dac7d7d1b7b09830))
* **dev-loop:** narrow the optional status-field match for mypy ([e35d039](https://github.com/mctlhq/mctl-agents/commit/e35d03974fe8e55ac92c4956247206f2790ffd37))
* **dev-loop:** never downgrade a resolved PR state to a 404 reference ([c594c24](https://github.com/mctlhq/mctl-agents/commit/c594c2430472892be7ac21bb87fd7306486bec81))
* **dev-loop:** ride out read outages; end the watch when the PR vanishes ([a15894b](https://github.com/mctlhq/mctl-agents/commit/a15894b7fea6092857c4496683f8f7de5e03e60f))
* **dev-loop:** stop masking activity bugs in the merge watch ([8b785a5](https://github.com/mctlhq/mctl-agents/commit/8b785a537837ce4a7955fc0d8d89c2431b77756e))
* **reconcile:** carry the visibility failure into log and result ([ad21063](https://github.com/mctlhq/mctl-agents/commit/ad21063f033564e9ff46fe4ed362c77ffb0a7e6b))
* **reconcile:** feed real active DevLoop ids into orphan detection ([da87247](https://github.com/mctlhq/mctl-agents/commit/da8724738afbd2e5d54a35616f1333957c53c075))
* **reconcile:** feed real active DevLoop ids into orphan detection ([7b56ba7](https://github.com/mctlhq/mctl-agents/commit/7b56ba71a18cc36f11f52bcc4ae2cb64dc818125)), closes [#151](https://github.com/mctlhq/mctl-agents/issues/151)
* **reconcile:** match the unpatched history's detect_orphans arity ([4c990cf](https://github.com/mctlhq/mctl-agents/commit/4c990cf7fa45f27712bedeadfc69d3d2587e78f4))
* **shepherd:** gate merges on chatgpt-codex-connector[bot] P1/P2 findings ([d7ba726](https://github.com/mctlhq/mctl-agents/commit/d7ba726e7e758e6484f91f1873102932f556d798))
* **shepherd:** gate merges on chatgpt-codex-connector[bot] P1/P2 findings ([13fe7a2](https://github.com/mctlhq/mctl-agents/commit/13fe7a2271c2b8f3da4f40a033509fa0986a6b55)), closes [#67](https://github.com/mctlhq/mctl-agents/issues/67)

## [1.30.0](https://github.com/mctlhq/mctl-agents/compare/1.29.4...1.30.0) (2026-08-29)


### Features

* **dev-loop:** atomic proposal approval via mctl-agents-approve CWFT ([832ed0b](https://github.com/mctlhq/mctl-agents/commit/832ed0b71a00f225790b504b8cb0ee3ff76dc1c0))
* **dev-loop:** atomic proposal approval via mctl-agents-approve CWFT ([6f96471](https://github.com/mctlhq/mctl-agents/commit/6f964715fe2886dddfe80a6318a254af961370fe))


### Bug Fixes

* **ci:** assert the image can import every entrypoint, not just build ([5faddd5](https://github.com/mctlhq/mctl-agents/commit/5faddd5afd227a3044a453bcc7b360f8a2d05bf7))
* **ci:** cover the Temporal worker in the entrypoint import smoke ([f2d6bc1](https://github.com/mctlhq/mctl-agents/commit/f2d6bc1def15c7c58c3a58d1a91b7a3efbb3e836))
* **dev-loop:** make approval durable before implementer resolve; review fixes ([638e32c](https://github.com/mctlhq/mctl-agents/commit/638e32cf46f4793636ce3e2e53ae60cb3e5438a4))
* **dev-loop:** replay-safe implementer-resolve position; approve-flow comment ([834ad4d](https://github.com/mctlhq/mctl-agents/commit/834ad4dcc78852c2b915722dc0cc5fcb282a96f2))
* **investigator:** harden prompt against issue-body injection; agy review fixes ([e1160c1](https://github.com/mctlhq/mctl-agents/commit/e1160c16c185dc2251f8569533281c862fc94592))
* **investigator:** neutralize forged delimiter tags in untrusted issue text ([0888b38](https://github.com/mctlhq/mctl-agents/commit/0888b38c4761d316c51f4cc7903685f4c56e3ea1))
* **investigator:** render the concrete workflow id in approve instructions ([0076378](https://github.com/mctlhq/mctl-agents/commit/0076378cdca52b25005c3d569d326299ffb1cfa5))
* **investigator:** strip forged delimiter tags carrying attributes ([53c5df7](https://github.com/mctlhq/mctl-agents/commit/53c5df7a1a3aee6a54b21ede82cd8a7d25ea3fc7))

## [1.29.4](https://github.com/mctlhq/mctl-agents/compare/1.29.3...1.29.4) (2026-08-28)


### Bug Fixes

* **shepherd:** recognize the P1:/P2: colon severity marker ([7239c31](https://github.com/mctlhq/mctl-agents/commit/7239c31ebb2001b7bc30458a33c542149d8e4d8d))

## [1.29.3](https://github.com/mctlhq/mctl-agents/compare/1.29.2...1.29.3) (2026-08-28)


### Bug Fixes

* **docker:** revert base image to python:3.12-slim ([8425347](https://github.com/mctlhq/mctl-agents/commit/842534757cbde4da2956252e1a3a74540cb93de1))

## [1.29.2](https://github.com/mctlhq/mctl-agents/compare/1.29.1...1.29.2) (2026-08-28)


### Bug Fixes

* **temporal:** address round-2 review on slug lookup ([a8d8553](https://github.com/mctlhq/mctl-agents/commit/a8d8553fc9b40a0463266ea486a8d591549e3c54))
* **temporal:** fail loudly when GITHUB_TOKEN is empty in find_proposal_slug ([a209232](https://github.com/mctlhq/mctl-agents/commit/a2092324241e97db140215a016b43f5f3c3f5a7b))
* **temporal:** gate slug lookup with workflow.patched for in-flight histories ([ec4d102](https://github.com/mctlhq/mctl-agents/commit/ec4d102eec050923b702f95eaf2f915265c8eae0))
* **temporal:** read token without env mutation; non-retryable deterministic listing errors ([bec3093](https://github.com/mctlhq/mctl-agents/commit/bec30935ed31c1ea04dee908d86cb5116657ec5a))
* **temporal:** scope DevLoop implement to its own proposal slug ([57c8b30](https://github.com/mctlhq/mctl-agents/commit/57c8b3086dedb19bca8c18d351279c4a587c3afe))

## [1.29.1](https://github.com/mctlhq/mctl-agents/compare/1.29.0...1.29.1) (2026-08-18)


### Bug Fixes

* **ci:** correct actions/checkout pin comments after the v7 bump ([47ca152](https://github.com/mctlhq/mctl-agents/commit/47ca15259822b24a422166207b17eee32e3e7992))
* **ci:** correct actions/checkout pin comments after the v7 bump ([a62654c](https://github.com/mctlhq/mctl-agents/commit/a62654c4d5d7deafefc8af4bf30a9c54befadb55))

## [1.29.0](https://github.com/mctlhq/mctl-agents/compare/1.28.2...1.29.0) (2026-08-16)


### Features

* **incident-responder:** state that incident data is untrusted input ([1cf68c2](https://github.com/mctlhq/mctl-agents/commit/1cf68c2cd97cf66c071117627d03272238acdd8f))
* **incident-responder:** take the shell away ([118a4ce](https://github.com/mctlhq/mctl-agents/commit/118a4ce77bddbea1066f17e4b9f854be0a93ee4c))


### Bug Fixes

* **incident-responder:** fence summary and labels, block-scalar the status note ([fb1c099](https://github.com/mctlhq/mctl-agents/commit/fb1c0991cf715d10962712221c6836406be9a577))
* **incident-responder:** fix the slug scratch path in code, not in the prompt ([50e6d5c](https://github.com/mctlhq/mctl-agents/commit/50e6d5cddcf365a710152d8aa2dcd2991a3a43d4))
* **incident-responder:** make the slug hash newline-insensitive ([49f9660](https://github.com/mctlhq/mctl-agents/commit/49f9660fd1231d876210dffd41b0fe93d42717ff))
* **incident-responder:** read escalated incidents, not only analyzing ([c0c07c6](https://github.com/mctlhq/mctl-agents/commit/c0c07c65622b6d568238b47f720e0bf80ea22f27))
* **incident-responder:** read escalated incidents, not only analyzing ([20a70a7](https://github.com/mctlhq/mctl-agents/commit/20a70a7e7003c049c80a2d1f035bec48c9dbfb91))
* **incident-responder:** scope the hash file to the state dir, strip backticks from logs ([b8cd3f4](https://github.com/mctlhq/mctl-agents/commit/b8cd3f4ec39ea103aef7a2862bb082cd763aed4a))
* **incident-responder:** stop instructing the agent to shell-quote incident IDs ([ddb6c16](https://github.com/mctlhq/mctl-agents/commit/ddb6c16c046b687fcd019e756a6a24822527c1d0))
* **incident-responder:** strip runs of three OR MORE backticks ([8ad8b72](https://github.com/mctlhq/mctl-agents/commit/8ad8b7294ef40aa87cc77791acf5f6ef452da1e5))
* **incident-responder:** unique filename for the slug-hash scratch file ([c27e83c](https://github.com/mctlhq/mctl-agents/commit/c27e83c3c9c9e760b00d415093deb2a8e7634d72))
* **run_all:** let a missing agent dir fail the incident-responder run ([b88ee5b](https://github.com/mctlhq/mctl-agents/commit/b88ee5b531396269476d1d40af7e9016a20b875d))

## [1.28.2](https://github.com/mctlhq/mctl-agents/compare/1.28.1...1.28.2) (2026-08-16)


### Bug Fixes

* **temporal:** don't log "spec is current" right after rewriting it ([9259605](https://github.com/mctlhq/mctl-agents/commit/92596050ce71a957242566019a00417174a248c8))
* **temporal:** run the incident responder in Argo, not inside the worker ([4407414](https://github.com/mctlhq/mctl-agents/commit/4407414faecb5392b9935eea07f4f1379db1d3af))
* **temporal:** run the incident responder in Argo, not inside the worker ([36cb72d](https://github.com/mctlhq/mctl-agents/commit/36cb72d10f955862f506ecd1dd4b21536ea59610))

## [1.28.1](https://github.com/mctlhq/mctl-agents/compare/1.28.0...1.28.1) (2026-08-15)


### Bug Fixes

* **orchestrator:** keep both streams in the CommandFailed message ([0924275](https://github.com/mctlhq/mctl-agents/commit/09242752a6a37ef0b33f558aa07c5bec8e74146f))
* **orchestrator:** keep captured output when a command times out ([43fe7db](https://github.com/mctlhq/mctl-agents/commit/43fe7db71af6a4247b449c351262b6f16bd94928))
* **orchestrator:** put stderr in the error when a subprocess fails ([8fd95e4](https://github.com/mctlhq/mctl-agents/commit/8fd95e4dd5a78b3fcd8ac013ecafadbeabcc7ca6))
* **orchestrator:** put stderr in the error when a subprocess fails ([87057e2](https://github.com/mctlhq/mctl-agents/commit/87057e2e845cec7985aabbe7cc97c7bebb827863))

## [1.28.0](https://github.com/mctlhq/mctl-agents/compare/1.27.0...1.28.0) (2026-08-14)


### Features

* **ci:** enable blocking mode for agy PR reviewer ([2c19574](https://github.com/mctlhq/mctl-agents/commit/2c19574618e230edbe0bcadaa5eb45e46cfc80ea))
* **ci:** enable blocking mode for agy PR reviewer ([8d6055d](https://github.com/mctlhq/mctl-agents/commit/8d6055d972e906b4812bd522b71a94a19eaa04a5))
* **temporal:** add DocsDeltaWorkflow and question-author agent ([d6e81bb](https://github.com/mctlhq/mctl-agents/commit/d6e81bb9ecc0809aa59c6a9aeefb4838a6f70146))
* **temporal:** add DocsDeltaWorkflow and question-author agent ([efaeccb](https://github.com/mctlhq/mctl-agents/commit/efaeccb06e8ffa3cef4961096e29323094300b73))


### Bug Fixes

* **manifest:** register question-author in agent inventory and satisfy CI checks ([91e7d05](https://github.com/mctlhq/mctl-agents/commit/91e7d05d6519aa62685a7ba9cf26a63016d120fb))
* **manifest:** register question-author in agent inventory and satisfy CI checks ([f80e1a7](https://github.com/mctlhq/mctl-agents/commit/f80e1a7e81c162d55979150f2af155d29e65e57f))
* **question-author:** compute sha256 dynamically from excerpt instead of empty default SHA ([64e1918](https://github.com/mctlhq/mctl-agents/commit/64e19189169baf65cd721acd50cf6f262fec9d83))
* **question-author:** compute sha256 dynamically from excerpt instead of empty default SHA ([a122720](https://github.com/mctlhq/mctl-agents/commit/a1227204edab96eec94520e6c6c69d1620d392f4))
* **question-author:** enforce 100% question.schema.json compliance in candidate generation ([62dd526](https://github.com/mctlhq/mctl-agents/commit/62dd52670a6c8fb9e70af4bcf56fbb96d10850b5))
* **question-author:** enforce 100% question.schema.json compliance in candidate generation ([539e126](https://github.com/mctlhq/mctl-agents/commit/539e1262c1fb6b54a12f5f1c02d01c14d4492d1a))
* **question-author:** enforce real R2 snapshot sha256 parameter in authoring pipeline ([5597dbd](https://github.com/mctlhq/mctl-agents/commit/5597dbd9253ea9b1f6824ae7c827bae4851de160))
* **question-author:** enforce real R2 snapshot sha256 parameter in authoring pipeline ([40f5cad](https://github.com/mctlhq/mctl-agents/commit/40f5cad5c5ec0986437ad91317026aba6455fa75))
* run the agents image as non-root ([f9b70db](https://github.com/mctlhq/mctl-agents/commit/f9b70db869834274da4b520813817523dd7e3da8))
* run the agents image as non-root ([d1dd7f4](https://github.com/mctlhq/mctl-agents/commit/d1dd7f447a8a3725d31481d23ef8289cc1672f89))
* **test:** add candidate verifier contract integration test ([db43147](https://github.com/mctlhq/mctl-agents/commit/db4314764d5ef8edd51dc4883eae2a9e0087a06a))
* **worker:** update issue poll schedule interval to 12 hours ([17915d0](https://github.com/mctlhq/mctl-agents/commit/17915d0ecbc253e66f16897bcef29b452118bf2c))
* **worker:** update issue poll schedule interval to 12 hours ([b12a85a](https://github.com/mctlhq/mctl-agents/commit/b12a85a1929ba09a65487db3b5d8d014ab9cde24))

## [1.27.0](https://github.com/mctlhq/mctl-agents/compare/1.26.0...1.27.0) (2026-08-07)


### Features

* **ci:** add context7.json and auto-reindex workflow on main push ([4ca5bf3](https://github.com/mctlhq/mctl-agents/commit/4ca5bf34196ca77c59627b3def92c980dbc9fcb7))
* **ci:** add context7.json and auto-reindex workflow on main push ([d9eddc8](https://github.com/mctlhq/mctl-agents/commit/d9eddc827e1003505913ef93f664d741dcbf6e20))


### Bug Fixes

* break circular import between worker and start/issue_poller via constants.py ([5d7f779](https://github.com/mctlhq/mctl-agents/commit/5d7f779c90159e6cfcde5733ee73228cc8bbcbc0))
* break circular import via constants.py ([302f499](https://github.com/mctlhq/mctl-agents/commit/302f49970db643b0bd27a14a55075964e84684bf))
* **ci:** fix auth fallback logic, activity mocks and mypy lints ([3c022e3](https://github.com/mctlhq/mctl-agents/commit/3c022e325ba59f41178d3368a1e05af780c60dd7))
* **ci:** fix auth unreachable code, activity mock, and mypy types in tests ([b602d51](https://github.com/mctlhq/mctl-agents/commit/b602d5181dff60a2e3963022a7cf14b45ff37049))
* **implementer:** correct stale workflows-permission note ([4fa73aa](https://github.com/mctlhq/mctl-agents/commit/4fa73aa15d1c7af65a0fb4adfbfd0a98d72183a5))
* **implementer:** forbid background deferral, fix stale workflows-permission note ([38ceffb](https://github.com/mctlhq/mctl-agents/commit/38ceffb67c3d7734f9101a9ff86d652bfa706487))
* **implementer:** forbid deferring work to a background task ([07bd3d8](https://github.com/mctlhq/mctl-agents/commit/07bd3d8f7f28f105aabf7b5b17b24eaa8e87b017))
* import poll in issue_poll activity ([5d9c1bd](https://github.com/mctlhq/mctl-agents/commit/5d9c1bdb540aa72a65832b4b38996dff169026d3))
* import poll instead of poll_once in issue_poll activity ([8b9afc6](https://github.com/mctlhq/mctl-agents/commit/8b9afc615cb05f4d8534ec5bf18b8721035e17df))
* import run_incident_responder in incidents activity ([e6be563](https://github.com/mctlhq/mctl-agents/commit/e6be563de17f8a95574cbd9aa69fbe04e54dd418))
* import run_incident_responder in incidents activity ([da49cbe](https://github.com/mctlhq/mctl-agents/commit/da49cbee1ee1c7bd72cf9b35f645c89037a4c151))
* import ScheduleAlreadyRunningError from temporalio.client ([e80c876](https://github.com/mctlhq/mctl-agents/commit/e80c87605145ffcd15c59b32df7ace472d91fcc9))
* import ScheduleAlreadyRunningError from temporalio.client ([e0e4d75](https://github.com/mctlhq/mctl-agents/commit/e0e4d75c9228a483ee1db88b434d9b889c195e3f))
* instruct implementer not to modify .github/workflows/ ([d8ab343](https://github.com/mctlhq/mctl-agents/commit/d8ab343720237a21c4c206cd3dd554fc57976b3d))
* instruct implementer not to modify .github/workflows/ ([36b211a](https://github.com/mctlhq/mctl-agents/commit/36b211a49be8a428254e42d568948bc508dfd621))
* ruff lint cleanups ([448c34e](https://github.com/mctlhq/mctl-agents/commit/448c34effb697d18920fb22f732eb66842e5f5cb))
* ruff lint cleanups ([c2c63bf](https://github.com/mctlhq/mctl-agents/commit/c2c63bf11b28cbf66c15daca9325e20990c934df))

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
