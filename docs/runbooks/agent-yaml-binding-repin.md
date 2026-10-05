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

The other five agents (`implementer`, `incident-responder`, `mentor`, `service-agent`,
`shepherd`) are not resolved declaratively at run time, but each has a binding in the same
catalog (`releases/shadow/<agent>.yaml`), and a release promotes an agent to production
only when that binding pins its `agent.yaml` (mctlhq/mctl-agents#470, see the last
section). So a byte change to any `agent.yaml` needs a re-pin of that agent's binding
before the release.

## What checks the pair

`tools/check_agent_bindings.py` compares every `agents/_manifests/*/agent.yaml` with its
binding on mctl-gitops `main` (mctlhq/mctl-agents#582). It has two parts, and it reports
each agent on its own line whatever the others did:

- **Every manifest** goes through `check_binding_hash.evaluate_promotion`, the function
  the release calls before it promotes an agent: the hash, the mirrored fields, and the
  pinned profile's version. A manifest directory with no binding at all is reported as
  `missing`. An agent listed in `UNBOUND_AGENTS` (`tools/publish_agent_release.py`, empty
  today) is the one exception: its missing binding is a warning, exactly as in the
  release.
- **`issue-investigator` also keeps `check_binding_hash.check()`**, the stricter check
  that loads the definition through the resolver's `load_definition`, as a declarative
  investigation does. It is reported as `issue-investigator (resolver check)`.

Both parts reuse the resolver's own hashing and binding parser. The tool runs in two
places:

- **The `binding hash` job in `pr-validation.yml`**, on every PR and every push to `main`.
  It tells the author at PR time that a binding needs a re-pin, or that a new manifest
  has no binding yet.
- **The `binding gate` job in `release-please.yml`.** The `release-please` job
  `needs` it, and that job does everything a release does: it creates the tag, dispatches
  `release-deploy` (which builds the image and bumps `agent_image` in every
  `cwft-mctl-agents-*.yaml`), and publishes and promotes the agents in the registry. If the
  gate fails, none of that happens, and production stays on the previous, matching release.
  The gate only runs when a merged release PR is still labelled `autorelease: pending`, that
  is, when the run is about to cut a release. It checks the manifests at that PR's merge
  commit, which is the commit that gets tagged: the job replaces `agents/_manifests` with
  that commit's, so an agent the release adds is checked and one it drops is not.

Both jobs write one line per agent to the log and a table to the job summary, each as soon
as that agent has been checked. A summary table with no `Exit` line under it is a run that
was cancelled before it finished: the rows are the agents it reached, not a verdict. The
job timeouts (35 and 40 minutes) are sized so that a rate-limited run ends as exit `2`
rather than as a cancellation.

Exit codes: `0` means every binding matches. `1` means at least one was observed and is
wrong: the hashes differ, a mirrored field differs, the binding's `spec.profile.version` is
not the catalog profile's, an `agent.yaml` does not resolve, or a manifest has no binding
(HTTP 404 on the binding's path, and the agent is not in `UNBOUND_AGENTS`). `2` means
nothing was observed to be wrong, but a binding, or the execution profile it names, could
not be read or validated: a network error, a non-200 response other than that 404, malformed
YAML, a missing or invalid `contentHash`, or a profile that is absent or has the wrong
`apiVersion`, `kind` or `metadata.name`, or a missing or unparseable `spec.version`. Code `2`
is never treated as a match. Re-run it once the read works.

When one agent mismatches and another could not be read, the exit code is `1`: an observed
mismatch is not masked by a failed read elsewhere, because it needs a re-pin that no re-run
supplies. Each agent's line still shows its own status, so read them all.

`tools/check_binding_hash.py` run directly still checks `issue-investigator` alone, with
the same codes, except that it reports a 404 on the binding as `2`.

## Procedure for a legitimate change

1. **Open the mctl-agents PR** that changes `agent.yaml`. The `binding hash` job turns red
   and names the agent; for `issue-investigator` the real-catalog resolver tests in `tests`
   turn red too. That is expected; review everything else as usual.

   The steps below are written for `issue-investigator`. For another agent they are the
   same with its name in the paths, and without the production window of steps 4 to 6:
   nothing hashes those manifests at run time, so merging their re-pin early breaks no
   running workflow. Their re-pin only has to be on mctl-gitops `main` before the release
   PR merges.
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

The job summary of the stopped run says which agent, and whether it is a `mismatch`,
`missing` or `unobservable`.

- **If the change was intended:** do steps 2 and 4 above, then re-run the failed workflow
  run with `gh run rerun <run-id> --failed`. The release PR is still labelled
  `autorelease: pending`, so the re-run cuts the release.
- **If an agent is `missing`:** its manifest is in the release commit and no
  `releases/shadow/<agent>.yaml` exists on mctl-gitops `main`. Add the binding (or restore
  it, if it was deleted or renamed) and re-run. Listing the agent in `UNBOUND_AGENTS` at
  this point is too late for this release: "Refresh agent registry" runs the tagged
  commit's copy of that list, not the one on `main`.
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
- **any environment other than `shadow`.** `releases/shadow/` is the only binding catalog,
  and the one the resolver reads.
- **the run-time resolver path of any agent other than `issue-investigator`.** Every
  agent's binding is compared with its manifest, but only `issue-investigator` gets
  `check()`, which loads the definition as the declarative resolver does, because today
  only the investigate CWFT sets a declarative resolver mode. `release-deploy` bumps
  `agent_image` in every `cwft-mctl-agents-*.yaml`, so when another CWFT goes declarative,
  extend `check()` to that agent in the same change.
- **a binding in mctl-gitops for an agent that has no manifest here.** The tool starts
  from the manifests; it does not list the catalog.

## Production promotion of every agent (mctlhq/mctl-agents#470)

The release-blocking gate above asks this question for every agent before the tag. The
step that acts on the answer is still this one, after the tag, and it reads mctl-gitops
again, so a refusal here means the binding changed, or could not be read, between the
two. `tools/publish_agent_release.py` promotes an agent to `production` only when
`check_binding_hash.evaluate_promotion` finds its binding on mctl-gitops `main`
(`releases/shadow/<agent>.yaml`, the same catalog the resolver reads) pinning the
exact `agent.yaml` in the tag, with the profile it pins present at that version. This
applies to every manifest, not only `issue-investigator`. The version is still
published, so it stays inactive until someone promotes it. Each agent's outcome
(promoted, or refused with the reason) is in the "Refresh agent registry" log and the
job summary.

- **Refused: missing**: the agent has no binding. This fails the step: every shipped
  agent has a binding since mctl-gitops#1683, so a 404 means one was deleted or
  renamed, or a manifest was added without one. The other agents in the release are
  still published and promoted, and production keeps the version it had for this one.
  To activate the release, add (or restore) the binding in mctl-gitops and promote the
  published version with `mctl_promote_agent`.

  The one exception is an agent listed in `UNBOUND_AGENTS`
  (`tools/publish_agent_release.py`), for which a missing binding is a warning and the
  step stays green. The set is empty today. It exists for an agent that has to ship
  ahead of its binding: list it there in the change that adds its manifest, and remove
  it in the mctl-agents change that follows its binding.
- **Refused: mismatch or unobservable**: a binding exists but is stale or disagrees
  with the release, or it (or its profile) could not be read. This fails the step.
  Reads are retried three times on connect errors, timeouts, 5xx and 429 (the
  secondary rate limit, honouring a `Retry-After` of up to 60s; a longer one
  refuses at once). 403 (the primary rate limit) and 404 are not retried.
  For a stale or mismatched binding, re-pin as described above. For an unreadable
  one, including a rate limit, wait until the read works. Then promote by hand:
  re-running the workflow does not repeat this step.
