# Changelog

## [1.39.0](https://github.com/mctlhq/mctl-agents/compare/1.38.0...1.39.0) (2026-09-03)


### Features

* **resolver:** resolve against the real mctl-gitops catalog ([09b8f2c](https://github.com/mctlhq/mctl-agents/commit/09b8f2c8b84e19b82bc970d08a90be549d60f966)), closes [#277](https://github.com/mctlhq/mctl-agents/issues/277)
* **temporal:** route submit_and_wait to the execution queue ([5e446ab](https://github.com/mctlhq/mctl-agents/commit/5e446aba96a4fde6f29bba3dcaa6867acc0ccb8a)), closes [#251](https://github.com/mctlhq/mctl-agents/issues/251)
* **tools:** add diagram drift detector and archify showcase validator ([4166c52](https://github.com/mctlhq/mctl-agents/commit/4166c52d54194be0bd1d671c3185398d0022927e))


### Bug Fixes

* **ci:** collect only this repo's tests ([b0a5f30](https://github.com/mctlhq/mctl-agents/commit/b0a5f30fc4f5cfede979266676ba115ee54faa23))
* **ci:** harden diagrams Pages index against agy review findings ([82d20ff](https://github.com/mctlhq/mctl-agents/commit/82d20ffc3cdb97b22ef7868c541cb645ffceb2a3))
* **reconcile:** do not move a status when the source issue is unreadable ([4986ec6](https://github.com/mctlhq/mctl-agents/commit/4986ec619070cf4375edd661065af24dfac93237))
* **reconcile:** read the source issue when a proposal has no PR ([edf2440](https://github.com/mctlhq/mctl-agents/commit/edf2440742e98fcede186daebd9146263f6828e4)), closes [#276](https://github.com/mctlhq/mctl-agents/issues/276)
* **resolver:** restore the definition pin, and reject partial constraints ([f0c5ab8](https://github.com/mctlhq/mctl-agents/commit/f0c5ab86df9572ca2d6afa9b49b090751ab2ae08)), closes [#277](https://github.com/mctlhq/mctl-agents/issues/277)
* **temporal:** correct what patched() does to an in-flight execution ([c16ec6e](https://github.com/mctlhq/mctl-agents/commit/c16ec6e5574a895f74ff94c2c950c5375c6be048))
* **tests:** the catalog-task test must fail, not no-op, under CI ([fafc8e5](https://github.com/mctlhq/mctl-agents/commit/fafc8e5cd79c0595c352e5da6c829932bbdf0d47)), closes [#277](https://github.com/mctlhq/mctl-agents/issues/277)
* **test:** the catalog test must fail, not skip, under CI ([234936b](https://github.com/mctlhq/mctl-agents/commit/234936b91a16c1defc37d834194cbfa5a750e96f))
* **tools:** address review on the diagram refresh loop ([e96abb1](https://github.com/mctlhq/mctl-agents/commit/e96abb1529027a7258cbf70764d466cdb4e45a0a))
* **tools:** keep release prose away from the refresh agent ([0ded0bb](https://github.com/mctlhq/mctl-agents/commit/0ded0bb9ef7ca40488f8794843553d17af293d04))
* **tools:** use the runner's Google Chrome for the diagram browser check ([5387ae7](https://github.com/mctlhq/mctl-agents/commit/5387ae7f5dec62c5290df5fff5c681b259d85572))
* **tools:** use the runner's Google Chrome for the diagram browser check ([7a9ff2c](https://github.com/mctlhq/mctl-agents/commit/7a9ff2cdb319be8df86b20d086c5a1e4c4f37fff))
* **validate:** an unresolvable agent is an error, and path handling is guarded ([55e25d8](https://github.com/mctlhq/mctl-agents/commit/55e25d85a9e3f82fba9a401c732dad68e1d6f600)), closes [#293](https://github.com/mctlhq/mctl-agents/issues/293)
* **validate:** check the catalog's model-policy task against this repo ([6d6fc78](https://github.com/mctlhq/mctl-agents/commit/6d6fc7873b5641c99cdfddfbd7e092ba2626ee45))
* **validate:** check the gitops catalog against the real builders ([c58a543](https://github.com/mctlhq/mctl-agents/commit/c58a5430adf311214d744e514cd8a24d2d9ad9ac)), closes [#277](https://github.com/mctlhq/mctl-agents/issues/277)
* **validate:** do not borrow the clean-env reload for the catalog check ([6683dfe](https://github.com/mctlhq/mctl-agents/commit/6683dfe3644f4263cd5de6bbbf6fe68505487189))
* **validate:** report a malformed catalog profile instead of crashing ([acfef53](https://github.com/mctlhq/mctl-agents/commit/acfef53195130142e73708a2bac37903dd57bcd0))
* **validate:** verify every binding's definition pin, not just the resolved one ([ea149f0](https://github.com/mctlhq/mctl-agents/commit/ea149f0134f602c38da309c13e62da6dd867258a)), closes [#293](https://github.com/mctlhq/mctl-agents/issues/293)

## [1.38.0](https://github.com/mctlhq/mctl-agents/compare/1.37.0...1.38.0) (2026-09-02)


### Features

* **worker:** export Temporal SDK metrics on the port the pod declares ([f864dbe](https://github.com/mctlhq/mctl-agents/commit/f864dbee77f2c619555b089e180c7ebc33952158)), closes [#252](https://github.com/mctlhq/mctl-agents/issues/252)

## [1.37.0](https://github.com/mctlhq/mctl-agents/compare/1.36.1...1.37.0) (2026-09-01)


### Features

* **reconcile:** submit the CWFT so found drift is actually written ([5af2082](https://github.com/mctlhq/mctl-agents/commit/5af2082db588b798d905bc39512c34062b273beb))


### Bug Fixes

* **reconcile:** a CWFT that ran and failed is not a written tick ([1f9bf93](https://github.com/mctlhq/mctl-agents/commit/1f9bf9361f5e1b3af41596bcc2e977a3293d3552))

## [1.36.1](https://github.com/mctlhq/mctl-agents/compare/1.36.0...1.36.1) (2026-09-01)


### Bug Fixes

* **reconcile:** let every concurrent read finish before raising ([292772b](https://github.com/mctlhq/mctl-agents/commit/292772b52e21832d04690fa420c36d46a45b8401))
* **reconcile:** read agents-state from GitHub, not from a checkout the worker lacks ([ec9acea](https://github.com/mctlhq/mctl-agents/commit/ec9acea6adc0f0bb5a04be1927bc07312c218445)), closes [#270](https://github.com/mctlhq/mctl-agents/issues/270)
* **reconcile:** skip one corrupt blob instead of failing the sweep ([a041735](https://github.com/mctlhq/mctl-agents/commit/a041735f390d030ea0cc1bcfce5da77d73ae4ac3))
* **reconcile:** wrap a malformed PR payload as a retryable read failure ([8cab345](https://github.com/mctlhq/mctl-agents/commit/8cab345cc8331fb1c8e12ae170ce5ed9df50ecb8))

## [1.36.0](https://github.com/mctlhq/mctl-agents/compare/1.35.0...1.36.0) (2026-09-01)


### Features

* **incidents:** pin the responder image and record the run ([85e2c95](https://github.com/mctlhq/mctl-agents/commit/85e2c95586027adf6b1cd95b1c53560113544039))


### Bug Fixes

* **incidents:** fail open when the registry lookup itself fails ([c545272](https://github.com/mctlhq/mctl-agents/commit/c54527218cf2816312622cdcf8cb8cc46acc8505))

## [1.35.0](https://github.com/mctlhq/mctl-agents/compare/1.34.0...1.35.0) (2026-09-01)


### Features

* **worker:** add role selection and slot limits for the queue split ([bc5f11d](https://github.com/mctlhq/mctl-agents/commit/bc5f11d7c977ca7d5b2d9d82ec182d743e073f86))
* **worker:** drain every worker on SIGTERM instead of dying mid-flight ([34ab05a](https://github.com/mctlhq/mctl-agents/commit/34ab05ae1b466d33705945632c660b473b60899c))


### Bug Fixes

* **investigator:** a lost proposal must crash, not return an error string ([f87613f](https://github.com/mctlhq/mctl-agents/commit/f87613f247f1d59cbb4f10c2acbb059a2fb7fe20))
* **investigator:** a missing descriptor is not an answer of "not ours" ([8e62497](https://github.com/mctlhq/mctl-agents/commit/8e62497d7cfe902ba85ce9602c80f8c54db5a364)), closes [#247](https://github.com/mctlhq/mctl-agents/issues/247)
* **investigator:** a triplet document must be a real file ([00bf5ab](https://github.com/mctlhq/mctl-agents/commit/00bf5ab3966df7a7fd9051394f88269614368872))
* **investigator:** bind the staging wrapper only after the move succeeds ([62694d4](https://github.com/mctlhq/mctl-agents/commit/62694d45c587071250f60c85b7adc8ac1cdaa09d))
* **investigator:** carry subdirectories into staging, not just files ([985a811](https://github.com/mctlhq/mctl-agents/commit/985a8118d87dc68697961a4a3fd3677cec440f39))
* **investigator:** carry the status file's mode across the swap too ([e35c51b](https://github.com/mctlhq/mctl-agents/commit/e35c51b8ebe3906b94c43fbe7441cf5fa6bf7f81))
* **investigator:** check what landed, not what was there a moment ago ([e39e1a5](https://github.com/mctlhq/mctl-agents/commit/e39e1a56b8289d14356cbe9381cf7061cd8e671f))
* **investigator:** clean up the clone wrapper on failure, mark success later ([1269861](https://github.com/mctlhq/mctl-agents/commit/1269861e141154000b1f8007d09c4e60d969e742))
* **investigator:** clear the triplet before the run and restore it on failure ([fa7485e](https://github.com/mctlhq/mctl-agents/commit/fa7485ede57d98f479149d87debae6d64a3f4c4a))
* **investigator:** close staging's parent, and say what these checks defend ([38453a9](https://github.com/mctlhq/mctl-agents/commit/38453a989a874e96418a4fc34b154a74fa5f0a9e))
* **investigator:** close the copy destination on a failed source open ([3bd23c4](https://github.com/mctlhq/mctl-agents/commit/3bd23c4535e47ef1451bb8d4ce4667022de1c66d)), closes [#247](https://github.com/mctlhq/mctl-agents/issues/247)
* **investigator:** decide the triplet after the publish, not before it ([9be80b6](https://github.com/mctlhq/mctl-agents/commit/9be80b65d4851dade744fa5905a59a1ff0a7e2b1)), closes [#247](https://github.com/mctlhq/mctl-agents/issues/247)
* **investigator:** discover the default file mode without clearing the umask ([1d1a8d4](https://github.com/mctlhq/mctl-agents/commit/1d1a8d4ec009aa2b368ef6b2283a5511d71e91b0))
* **investigator:** identify staging by a held fd, not by inode number alone ([0d2b045](https://github.com/mctlhq/mctl-agents/commit/0d2b0459108f45c2c6bd86e15067f3f58df159c8)), closes [#247](https://github.com/mctlhq/mctl-agents/issues/247)
* **investigator:** key the proposal directory on the issue number ([2bbf540](https://github.com/mctlhq/mctl-agents/commit/2bbf54026d17570666061de5d5aa98ce27f70d03))
* **investigator:** make the triplet rollback an exact inverse ([7f4e24e](https://github.com/mctlhq/mctl-agents/commit/7f4e24e972d4865efea34045535e87c7d4f072db))
* **investigator:** merge carried-forward folders leaf by leaf ([f364a4d](https://github.com/mctlhq/mctl-agents/commit/f364a4dba33b1d19d17d0bf374af008e23c30e3a))
* **investigator:** move staging out of the agent's reach before writing ([e7a0fd0](https://github.com/mctlhq/mctl-agents/commit/e7a0fd0686f66c810b62c5f6a1e52da9f83a74a7))
* **investigator:** never follow a symlink while carrying a proposal forward ([7a8e98d](https://github.com/mctlhq/mctl-agents/commit/7a8e98d05e5f597093a43ff0d181c4497ad50fb0))
* **investigator:** never open a special file, and carry modes by rule ([3473691](https://github.com/mctlhq/mctl-agents/commit/347369160663aaf9abedb9efde3a6f1dcabf01a9))
* **investigator:** open the copy source without following it either ([c22ed3b](https://github.com/mctlhq/mctl-agents/commit/c22ed3bca92a4eb947125de754301fb29ed33a5c))
* **investigator:** preserve the original failure and never leak the aside copy ([92aa277](https://github.com/mctlhq/mctl-agents/commit/92aa277dce59b81e98e4ad15c7546de4b6b83501))
* **investigator:** publish by swapping directories, not file by file ([bda470e](https://github.com/mctlhq/mctl-agents/commit/bda470e7380ae54fa1aed04ac81547563b1d628f))
* **investigator:** publish through a directory descriptor, not a path ([db49ca1](https://github.com/mctlhq/mctl-agents/commit/db49ca14b96086b992df530fdec5f27ff7bd3e22))
* **investigator:** re-check the proposal status immediately before publishing ([f951a6c](https://github.com/mctlhq/mctl-agents/commit/f951a6ce23d25b7a17ec324d8112546729b8c19f))
* **investigator:** refuse a proposal that approved itself ([9eb3bbb](https://github.com/mctlhq/mctl-agents/commit/9eb3bbb3a4805f84627b74e8fa2c6653a85ca38a)), closes [#247](https://github.com/mctlhq/mctl-agents/issues/247)
* **investigator:** refuse to publish a staging directory that was replaced ([be9e6d4](https://github.com/mctlhq/mctl-agents/commit/be9e6d414ac65eaa4f7c3d9da2e45dd6dcd48234))
* **investigator:** reject a status payload that is not a mapping ([c7756b7](https://github.com/mctlhq/mctl-agents/commit/c7756b7cc1144593f81d485142ff5e4b3e2827db)), closes [#247](https://github.com/mctlhq/mctl-agents/issues/247)
* **investigator:** roll back from the rename, not from the swap ([2d8be81](https://github.com/mctlhq/mctl-agents/commit/2d8be81d98c381727fd6e2400dd0a7bf8acd1435))
* **investigator:** stop resolving paths twice inside a writable directory ([d16a882](https://github.com/mctlhq/mctl-agents/commit/d16a88261ebc346f10236c83596aec5119ae77af))
* **investigator:** stop taking the status file's mode from the agent ([3422ce0](https://github.com/mctlhq/mctl-agents/commit/3422ce0da73e4328ad08bfe143a2b7c4ba764a3f)), closes [#247](https://github.com/mctlhq/mctl-agents/issues/247)
* **investigator:** stop the rollback deleting documents it never observed ([d694c53](https://github.com/mctlhq/mctl-agents/commit/d694c5352c99a780ca8c1b40e8ff735b589b9478))
* **investigator:** type-check the nested status blocks, and fail closed ([4fc64cc](https://github.com/mctlhq/mctl-agents/commit/4fc64cc4442a2b3b2c5aa799f51b168f2ae282a7)), closes [#247](https://github.com/mctlhq/mctl-agents/issues/247)
* **investigator:** verify the aside copy, and recover from a rejected publish ([cf88c9c](https://github.com/mctlhq/mctl-agents/commit/cf88c9c5ec97eca472b796dd32f460e26d7d1132))
* **investigator:** verify the whole published status, not just its status ([f3108d7](https://github.com/mctlhq/mctl-agents/commit/f3108d7bc159b8ff0dc986e73e9735b80e173109)), closes [#247](https://github.com/mctlhq/mctl-agents/issues/247)
* **investigator:** verify this run wrote the triplet, not that files exist ([de8f86f](https://github.com/mctlhq/mctl-agents/commit/de8f86f7cd17f195073b227fa54fc41be22072b3))
* **investigator:** write .status.yaml atomically, read the triplet before removing it ([c7575f5](https://github.com/mctlhq/mctl-agents/commit/c7575f5a31c507f9231395e55872ed5b8843d01b))
* **shepherd:** fence review findings as data; skip SDK auth on dry-run ([89afc8c](https://github.com/mctlhq/mctl-agents/commit/89afc8c0a9bc2480f3c62f7e21e393281b95d92e))
* **shepherd:** leave a marker where a fence tag was, not a gap ([4236f6d](https://github.com/mctlhq/mctl-agents/commit/4236f6d1fd905d46b29de391a9e1a8b4a5ddd169))
* **shepherd:** strip a fence tag that never closes, in both guards ([b5c0e23](https://github.com/mctlhq/mctl-agents/commit/b5c0e23bafbdedb9a6668d83a910bbf5496bed59))
* **worker:** cap the drain when a worker died rather than was signalled ([644a8b6](https://github.com/mctlhq/mctl-agents/commit/644a8b6ae636033586204d69dd404387cc4a841a))
* **worker:** crash when a worker dies alone, not just on SIGTERM ([a10cb67](https://github.com/mctlhq/mctl-agents/commit/a10cb673ba1e946c4939611847bbfea8c3e7fcfa))
* **worker:** keep the control limit at the SDK default until the flip ([fc1363e](https://github.com/mctlhq/mctl-agents/commit/fc1363e917517ac853e0479c759b16ed7fc1a273))
* **worker:** leave the control workflow-task ceiling unset until the flip ([e65a2d9](https://github.com/mctlhq/mctl-agents/commit/e65a2d904bf256f39fd720cbca318ce5d8f21d06))
* **worker:** make `all` poll both queues; correct the metrics claim ([65be8d5](https://github.com/mctlhq/mctl-agents/commit/65be8d57cea3c60e37a4c46035c69aa12ee5ba2f))
* **worker:** make a failed schedule registration loud, not fatal ([f9c0b48](https://github.com/mctlhq/mctl-agents/commit/f9c0b48d55ac271a05288b369a6952d3d0d2e5db))
* **worker:** one deadline over the drain, and report what would not stop ([6c86658](https://github.com/mctlhq/mctl-agents/commit/6c866584f59f7d719172594d689ca087b79eb64c))

## [1.34.0](https://github.com/mctlhq/mctl-agents/compare/1.33.0...1.34.0) (2026-08-31)


### Features

* **dev-loop:** bounded incident watch after the rollout ([8867429](https://github.com/mctlhq/mctl-agents/commit/8867429b18bdeab9de64c1512a7a29d10c8a13f7))
* **dev-loop:** observe the release and verify the rollout ([79c0e00](https://github.com/mctlhq/mctl-agents/commit/79c0e00e2d2e7cb08a182965399829fcd1a21525))
* **dev-loop:** require a registry-pinned image for every agent ([336bed6](https://github.com/mctlhq/mctl-agents/commit/336bed6154f395f3cf6bef6f3a2530509e02d2b4))
* **release:** refresh the agent registry on every release ([f669eba](https://github.com/mctlhq/mctl-agents/commit/f669eba5d4b368ebf594181f7dbc892eb6de641c))
* **release:** take the registry token from Vault via OIDC ([706101a](https://github.com/mctlhq/mctl-agents/commit/706101a101147803e169bff8b601a99a8a4e6ebf))


### Bug Fixes

* **dev-loop:** catch CancelledError when settling a tick ([077e149](https://github.com/mctlhq/mctl-agents/commit/077e1490a781f5e7abf305367e104b6029756df6))
* **dev-loop:** decline the shepherd claim instead of failing the loop ([86941ed](https://github.com/mctlhq/mctl-agents/commit/86941ed6bd4c83dc3eca592ee4239edaf1e979f0))
* **dev-loop:** do not label a broken read as no-release ([3b4eeeb](https://github.com/mctlhq/mctl-agents/commit/3b4eeebe13908bb2fd457beaa3c12e2573ee51ba))
* **dev-loop:** drain a finished tick before dropping its reference ([e66c6e6](https://github.com/mctlhq/mctl-agents/commit/e66c6e6cfa42ae45bfee70bb24398e2e57d782bc))
* **dev-loop:** four correctness fixes in the deploy stages ([6e1514f](https://github.com/mctlhq/mctl-agents/commit/6e1514f32a1edd8283008aadc8b7649524d5e5f8))
* **dev-loop:** gate the shepherd before the claim, keep failures restartable ([ab8a1eb](https://github.com/mctlhq/mctl-agents/commit/ab8a1ebd9f6fb97d7f12a408c7eba2cad6411ac7))
* **dev-loop:** keep the shepherd gate fail-open when the registry is down ([81262f0](https://github.com/mctlhq/mctl-agents/commit/81262f0714b0646eea99577d19ff9c94323f9621))
* **dev-loop:** keep the timestamp helper total, log the real window ([975a6ba](https://github.com/mctlhq/mctl-agents/commit/975a6baecdcc1a66efd0c91f5dcdedac70bb01b0))
* **dev-loop:** never let a timestamp compare wedge the workflow ([f4457d3](https://github.com/mctlhq/mctl-agents/commit/f4457d3b8b666359b56b45cec7cdd48ce62343c3))
* **dev-loop:** open the incident window before the deploy observation ([d57007b](https://github.com/mctlhq/mctl-agents/commit/d57007b512e512c751d9bc0cd9581fe4233cee06))
* **dev-loop:** refuse a deploy target that is not a safe path segment ([322e837](https://github.com/mctlhq/mctl-agents/commit/322e837677606b025b79d65c96a9050fc928361e))
* **dev-loop:** report the real incident window, keep numeric ids ([c97f3f9](https://github.com/mctlhq/mctl-agents/commit/c97f3f9add062c397b241991e4f4255b98377bf4))
* **dev-loop:** report truncated incident windows and odd id types ([377629a](https://github.com/mctlhq/mctl-agents/commit/377629af32bab30aeddef41eed2b6c790ebb7408))
* **dev-loop:** run the in-loop shepherd tick concurrently ([ddae339](https://github.com/mctlhq/mctl-agents/commit/ddae339a391d3c2cbe2ef93c4ed603dfb7d20340))
* **dev-loop:** tell a transient read from a bug in the deploy stages ([93dd0e3](https://github.com/mctlhq/mctl-agents/commit/93dd0e3bffb3c9942559ef212c1928d8d2a6a63a))
* **release:** check out the tag, stop globbing across directories, add tests ([518ae9a](https://github.com/mctlhq/mctl-agents/commit/518ae9aa12c5565713e23ab5a07b9b461490b805))
* **release:** drop the dead digest lookup, isolate per-agent failures ([254222c](https://github.com/mctlhq/mctl-agents/commit/254222c014432e3032b2d2e62d90804f608438f3))
* **release:** give the registry step its own interpreter, unquote tree paths ([37f0db1](https://github.com/mctlhq/mctl-agents/commit/37f0db173685e962353e1c632b6706fb8e9e65f1))
* **release:** make the digest unambiguous, isolate every failure kind ([3c64b73](https://github.com/mctlhq/mctl-agents/commit/3c64b738a280331a157ab122c708a833338170a0))

## [1.33.0](https://github.com/mctlhq/mctl-agents/compare/1.32.0...1.33.0) (2026-08-29)


### Features

* **dev-loop:** shepherd inside DevLoop, cron becomes a sweeper ([a2b4fd2](https://github.com/mctlhq/mctl-agents/commit/a2b4fd2ecfa92986a0d9d9337cd61fa76b1bdfeb))


### Bug Fixes

* **dev-loop:** bound the ownership pass, fail open on every path ([52a51e4](https://github.com/mctlhq/mctl-agents/commit/52a51e45552b41498254f7a540dd7c831cd35fdf))
* **dev-loop:** build the liveness request inside the guard ([dffb35b](https://github.com/mctlhq/mctl-agents/commit/dffb35b0d543e1875c1e269777d78132d6a1cda0))
* **dev-loop:** make the ownership budget wall-clock, cover the tick cap ([3a84878](https://github.com/mctlhq/mctl-agents/commit/3a84878fe340234942b6a852995444035eeff20a))
* **dev-loop:** ownership asks the workflow, not its status ([e6e78e1](https://github.com/mctlhq/mctl-agents/commit/e6e78e1462029607a3b1138b7ea8a46fc6dfd5d3))
* **dev-loop:** review follow-ups on the sweeper ([f330d33](https://github.com/mctlhq/mctl-agents/commit/f330d3393d73025bc11a135ed3f1b25f22de07f1))

## [1.32.0](https://github.com/mctlhq/mctl-agents/compare/1.31.0...1.32.0) (2026-08-29)


### Features

* **agents:** define AgentDefinition and ExecutionProfile contract ([7bf55c2](https://github.com/mctlhq/mctl-agents/commit/7bf55c241efafa588999e09f93752003fe13a35a))
* **agents:** issue-226-architecture-agent-platform-define-agent ([7bf55c2](https://github.com/mctlhq/mctl-agents/commit/7bf55c241efafa588999e09f93752003fe13a35a))
* **agents:** issue-226-architecture-agent-platform-define-agent ([7dfaf06](https://github.com/mctlhq/mctl-agents/commit/7dfaf06a511cebb2c8b6f5f178e6fac0412cc071))

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
