# #848 review record template (blank)

STATUS: PROPOSED / NOT_RUN

Refs mctlhq/mctl-agents#848 and #974. Companion to `protocol.md`.

This is a blank form. Every value cell is empty on purpose. Do not fill it
with example, dummy or placeholder results. Do not enter participant names,
real proposal content, credentials, tokens, user identities or other private
information. One record per review of one unit.

Legend. Req: R = required, O = optional. Who: C = coordinator, V = reviewer,
S = scorer, P = artifact producer run. A value never captured is written
`unobserved`. A value that could not be determined is `unknown`. Neither is
ever `0` or `pass`.

The instrument does not capture section reads or times automatically. Each
time and read field below is filled by a person, and "Capture method" says
how.

## A. Unit and binding

| field | Req | Who | value |
|---|---|---|---|
| unit ID | R | C | |
| unit kind (synthetic / real) | R | C | |
| control type (clean / harmful / stale / unknown / none) | R for synthetic | C | |
| pinned target repo SHA (40 hex) | R | C | |
| proposal digest (from `content_digest_of`) | R | C | |
| artifact digest (arm with artifact only) | R for that arm | P | |
| freshness at review (`fresh` / `stale` / `unknown`) | R for that arm | C | |
| frozen oracle version and digest (synthetic only) | R for synthetic | C | |
| unit author role holder (pseudonym) | R for synthetic | C | |
| oracle author role holder (pseudonym) | R for synthetic | C | |

## B. Reviewer, arm and order

| field | Req | Who | value |
|---|---|---|---|
| reviewer pseudonym | R | C | |
| arm (baseline / artifact) | R | C | |
| review index for this reviewer | R | C | |
| order within arm | O | C | |
| collapsed roles, if any (say which) | R | C | |

## C. Time capture

| field | Req | Who | value |
|---|---|---|---|
| capture method (observed by coordinator / self-reported / other) | R | C | |
| start time | R | C or V | |
| end time at decision | R | C or V | |
| pauses or interruptions | O | C or V | |
| common work time (reading the proposal) | R | C or V | |
| artifact reading and checking time (artifact arm) | O | C or V | |
| fallback time (included in total, not added again) | O | C or V | |
| decision time | O | C or V | |
| total reviewer time | R | C | |
| end-to-end turnaround, own timestamps | O | C | |
| machine latency (artifact generation) | O | P | |
| tokens and cost (artifact generation) | O | P | |

Machine latency, tokens and cost are not reviewer time and are not summed into
it. Fallback is part of total reviewer time and appears once there.

## D. What the reviewer did

| field | Req | Who | value |
|---|---|---|---|
| full proposal opened (yes / no / unobserved) | R | V or C | |
| how that was captured | R | C | |
| artifact pointers used (list refs, or none) | O | V | |
| fell back to full proposal (yes / no / unobserved) | O | V or C | |
| sections read, if self-reported | O | V | |

## E. Decision and risks noticed

| field | Req | Who | value |
|---|---|---|---|
| decision (approve / request changes) | R | V | |
| risks noticed (short free text; no real proposal content) | R | V | |
| reviewer confidence | O | V | |

Risks noticed is not full ground truth.

## F. Scoring (synthetic only)

| field | Req | Who | value |
|---|---|---|---|
| link to oracle assessment (access-separated, not stored here) | R for synthetic | S | |
| scorer pseudonym | R for synthetic | S | |
| scorer blind to arm (yes / no) | R for synthetic | S | |
| oracle classes excluded from scoring (report, not pass) | R for synthetic | S | |

Do not write defect counts or match results in this file. They stay in the
access-separated assessment.

## G. Missingness and provenance

| field | Req | Who | value |
|---|---|---|---|
| fields `unobserved` (list) | R | C | |
| fields `unknown` (list) | R | C | |
| artifact provenance (producer, inputs, pinned only, no implementation visible) | R for artifact arm | P | |
| deviations from protocol | R | C | |
| authorization reference for this run (execution authorization ID) | R | C | |
