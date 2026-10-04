# Re-pinning the issue-investigator binding after an agent.yaml change

Context: mctlhq/mctl-agents#565, mctl-gitops#1585.

`mctl-agents-investigate` runs `ISSUE_INVESTIGATOR_RESOLVER_MODE=declarative`. Every
run hashes `agents/_manifests/issue-investigator/agent.yaml` inside the image it runs and
compares the result with `spec.sourceManifest.contentHash` in mctl-gitops
`platform-gitops/agent-platform/releases/shadow/issue-investigator.yaml`. The run reads
that binding from a fresh clone of mctl-gitops `main` when it starts. If the hashes
differ, `orchestrator/resolver.py` raises `ResolverError` before the model runs, and the
investigation fails.

So any byte change to `agent.yaml`, comments and whitespace included, needs a matching
re-pin in mctl-gitops.

## What checks the pair

`tools/check_binding_hash.py` compares the local `agent.yaml` hash with the binding on
mctl-gitops `main`. It reuses the resolver's own hashing and binding parser. It runs in
two places:

- **The `binding hash` job in `pr-validation.yml`**, on every PR and every push to `main`.
  It tells the author at PR time that the binding needs a re-pin.
- **The `binding gate` job in `release-please.yml`.** The `release-please` job
  `needs` it, and that job does everything a release does: it creates the tag, dispatches
  `release-deploy` (which builds the image and bumps `agent_image` in every
  `cwft-mctl-agents-*.yaml`), and publishes and promotes the agents in the registry. If the
  gate fails, none of that happens, and production stays on the previous, matching release.
  The gate only runs when a merged release PR is still labelled `autorelease: pending`, that
  is, when the run is about to cut a release. It checks `agent.yaml` at that PR's merge
  commit, which is the commit that gets tagged.

Exit codes: `0` means the binding matches. `1` means it does not: the hashes differ, a
mirrored field differs, the binding's `spec.profile.version` is not the catalog profile's,
or the local `agent.yaml` does not resolve. `2` means the binding, or the execution profile
it names, could not be read or validated: a network error, a non-200 response, malformed
YAML, a missing or invalid `contentHash`, or a profile with the wrong `apiVersion`, `kind`
or `metadata.name`, or a missing or unparseable `spec.version`. Code `2` is never treated as
a match. Re-run it once the read works.

## Procedure for a legitimate change

1. **Open the mctl-agents PR** that changes `agent.yaml`. The `binding hash` job and the
   real-catalog resolver tests in `tests` turn red. That is expected; review everything
   else as usual.
2. **Prepare the mctl-gitops re-pin PR, but do not merge it yet.** Take the hash of the
   PR's final `agent.yaml`:

   ```sh
   git show <pr-head-sha>:agents/_manifests/issue-investigator/agent.yaml | shasum -a 256
   ```

   Set `spec.sourceManifest.contentHash: "sha256:<hex>"`, bump `bindingRevision`, set
   `previousBindingRevision`, and add a `history` entry. If the PR changes after this
   step, recompute the hash.

   The hash is not the only thing the binding mirrors. If the PR also changes any of
   these in `agent.yaml`, update the binding to match, or every run fails on them even
   with a correct hash:

   | `agent.yaml` | binding |
   |---|---|
   | `metadata.name` | `spec.definition.name` |
   | `spec.executionProfileRef.name` | `spec.profile.name` |
   | `spec.executionProfileRef.compatibility` | `spec.definition.profileCompatibility` |

   `spec.profile.version` must also equal the catalog profile's `spec.version` and satisfy
   that range; the gate reads the profile from the same mctl-gitops ref to check it.

   The gate runs the resolver's own checks for all of these, not only the hash.
3. **Merge the mctl-agents PR.** Production is unaffected: it still runs the old image
   against the old pin. release-please folds the change into its release PR. The
   `binding hash` check on `main` stays red until step 4.
4. **Merge the re-pin.** The production window opens here. Every new investigation still
   runs the old image, whose `agent.yaml` no longer matches the pin, and fails with
   `ResolverError`. Runs that started earlier already hold their own clone and are not
   affected.
5. **Merge the release PR immediately.** The `binding gate` now passes, the release is
   cut, `release-deploy` builds the image and bumps the CWFTs, and the registry promotes
   `issue-investigator`.
6. **The window closes** when both of these are true:
   - the new image exists in GHCR, which registry-pinned DevLoop runs need, since promotion
     happens before the build finishes;
   - Argo CD has synced the bumped `cwft-mctl-agents-investigate`, which submissions that
     do not pin an image need.

   Check with `mctl_resolve_agent issue-investigator` and the CWFT's `agent_image` default.

**How long the window is:** the release-please run, plus the image build (recent
`release-deploy admins/mctl-agents` runs took about 1.5 to 2.5 minutes: 1.64.1, 1.65.0
and 1.66.0), plus the Argo CD sync of the CWFT. It also includes however long the
release PR takes to merge after the re-pin, so do steps 4 and 5 back to back. Investigations
triggered inside the window fail and have to be re-triggered. Pick a quiet time.

Reversing the order does not avoid the window, it moves it. If the release goes first,
the new image runs against the old pin and fails in the same way. The gate refuses that
order anyway.

## The window cannot be closed with a single pinned hash

The binding accepts exactly one `agent.yaml` hash, and production switches from the old
image to the new one over an interval rather than at one instant. Whichever hash is
pinned during that interval, one of the two images fails. Ordering the steps cannot fix
that; it can only shorten the interval.

**A proposal, not implemented:** let the binding accept a *next* hash during a
transition, for example `spec.sourceManifest.nextContentHash` or a two-entry
`contentHashes` list. The resolver would accept either hash, and the gate would require
the release's hash to be one of them. The procedure would become:

1. add the new hash as `next`; both images now resolve;
2. release and promote;
3. once nothing runs the old image, promote `next` to `contentHash` and drop the old hash.

That has no failure window. It needs a binding-schema and validator change in
mctl-gitops and a resolver change here, and the owner has to decide on it.

## If the gate stopped a release

Telegram reports "release stopped by the binding gate", and no tag, image or promotion
exists.

- **If the change was intended:** do steps 2 and 4 above, then re-run the failed workflow
  run with `gh run rerun <run-id> --failed`. The release PR is still labelled
  `autorelease: pending`, so the re-run cuts the release.
- **If it was not intended:** revert the `agent.yaml` change on `main`. The next push
  re-runs the gate.
- **If the error is `ambiguous version` (binding `spec.profile.version` vs the catalog
  profile):** no `agent.yaml` change or hash re-pin clears it. The profile was versioned
  without re-binding. Fix it in mctl-gitops by re-binding to the new version or reverting
  the bump. Until then every mctl-agents release is blocked, which is correct: production
  would fail the same way.
- **On exit code 2** (unknown): read the error. A GitHub outage or rate limit just needs a
  re-run. A malformed binding or execution profile on mctl-gitops `main` needs fixing there
  first, because the resolver would fail on it at run time too.

## Paths this gate does not cover

These bypass the release workflow. The resolver's own run-time check still applies, so
they fail closed rather than run with the wrong definition, but nothing warns about them
ahead of time:

- a manual `gitops-bump.yaml` or `release-deploy.yaml` dispatch in mctl-gitops with an
  mctl-agents tag;
- a manual `mctl_publish_agent_version` or `mctl_promote_agent`, or a rollback, to a
  version whose `agent.yaml` differs from the pin;
- an explicit `agent_image` passed at submission;
- a change to the binding or the execution profile on the mctl-gitops side. The
  `binding hash` job only runs on mctl-agents events, so a gitops edit that breaks the pin
  shows up only on the next mctl-agents PR or push, or at run time.
- **a `promptSources` path in `agent.yaml` that is missing from the image.** The gate
  covers the binding-vs-definition pair, not every `ResolverError` a release can
  introduce; the `tests` job catches this one at PR time, but not in front of the release.
- **any agent other than `issue-investigator`, or any environment other than `shadow`.**
  The tool checks exactly that one binding, because today only the investigate CWFT sets
  a declarative resolver mode. `release-deploy` bumps `agent_image` in every
  `cwft-mctl-agents-*.yaml`, so when another CWFT goes declarative, extend
  `tools/check_binding_hash.py` to cover its binding in the same change.

## Production promotion of every agent (mctlhq/mctl-agents#470)

Separately from the release-blocking gate above, `tools/publish_agent_release.py`
promotes an agent to `production` only when
`check_binding_hash.evaluate_promotion` finds its binding on mctl-gitops `main`
(`releases/shadow/<agent>.yaml`, the same catalog the resolver reads) pinning the
exact `agent.yaml` in the tag, with the profile it pins present at that version. This
applies to every manifest, not only `issue-investigator`. The version is still
published, so it stays inactive until someone promotes it. Each agent's outcome
(promoted, or refused with the reason) is in the "Refresh agent registry" log and the
job summary.

- **Refused: missing**: the agent has no binding. For the agents in
  `UNBOUND_AGENTS` (`tools/publish_agent_release.py`) this is a warning, and the step
  stays green. For any other agent it means a binding was deleted or renamed, and the
  step fails. Either way, production keeps the version it had. To activate the
  release, add the binding in mctl-gitops, remove the agent from `UNBOUND_AGENTS` in
  the same mctl-agents change that follows it, and promote the published version with
  `mctl_promote_agent`.
- **Refused: mismatch or unobservable**: a binding exists but is stale or disagrees
  with the release, or it (or its profile) could not be read. This fails the step.
  Reads are retried three times on connect errors, timeouts, 5xx and 429 (the
  secondary rate limit, honouring a `Retry-After` of up to 60s; a longer one
  refuses at once). 403 (the primary rate limit) and 404 are not retried.
  For a stale or mismatched binding, re-pin as described above. For an unreadable
  one, including a rate limit, wait until the read works. Then promote by hand:
  re-running the workflow does not repeat this step.
