# kin-actions

> Shared GitHub Actions workflows for Kin registry releases and dependency waves.

Reusable workflows for enforcing version movement, publishing to the Kin cargo
registry, verifying published crates, and keeping downstream dependency pins in
sync. Each Kin repository keeps a thin workflow wrapper and pins a semver tag of
`kin-actions`, so release enforcement lives in this one repository.

This is shared CI and release infrastructure for the Kin ecosystem, not a product
surface. In the ecosystem manifest (`kin/docs/ecosystem-manifest.json`) it is
`layer: infrastructure`, `role: shared-ci-and-release-workflows`.

Published versions are listed on the
[GitHub tags page](https://github.com/kinlab-ai/kin-actions/tags).

[![Part of Kin](https://img.shields.io/badge/part%20of-Kin-6E56CF.svg)](https://github.com/kinlab-ai/kin)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

## What is Kin?

Kin is a graph-native code repository for AI agents and the people who build with
them. It stores source, recorded code relationships, and versioned history as
repository state. The graph is the repository model, not a search index maintained
beside another repository.

Start at **[kinlab-ai/kin](https://github.com/kinlab-ai/kin)** · **[kinlab.ai](https://kinlab.ai)**

## Release contract

- Ordinary source PRs do not hand-edit package versions in train mode. Trusted
  `main` drift is coalesced into one protected `automation/release-next` PR.
- Source drift defaults to a patch. A `Kin-Release-Intent: minor` or
  `Kin-Release-Intent: major` trailer on a landed first-parent commit escalates
  intent; the highest immutable trailer since the last release wins. Mutable
  PR labels have zero release-intent authority.
- The train changes only the exact package manifest and tracked root lockfile
  discovered from Cargo metadata. Manual version edits and extra generated-PR
  paths fail closed.
- Only the generated version-moving `main` commit can publish. A tag delivery
  never re-enters Cargo publication.
- Publication is followed by a fresh registry-only consumer build before the
  immutable tag and downstream notification are admitted.
- Dependency waves update their complete declared fanout. The workflow-pin wave
  globally validates all 15 repositories and 35 live workflow paths before its
  first write, records later non-versioned pins as stage-local activation
  holds, pilots one version on `kin-blobs`, then advances one bottom-up consumer
  at a time. A pin PR keeps auto-merge off until its exact generated
  head has a complete green check set. Each Cargo caller must then prove the
  exact landed main SHA through all four registry-release jobs before the next
  consumer can begin.

Registry publication, release-tag minting, GitHub Release creation, and
downstream dispatch are separate durable stages. The automatic recovery
controller resumes only missing post-publish stages, never republishes or moves
a tag, and maintains one terminal failure issue per package. Terminal issue
reconciliation uses the caller-scoped `GITHUB_TOKEN` with Issues write, not the
release App, so missing or mis-scoped App credentials are themselves durable
incidents instead of red runs that require polling. Healthy no-op runs close a
prior package issue.

Recovery exports the last proven monotonic boundary:
`registry-available`, `consumer-proven`, `tag-present`, `release-finalized`,
`downstreams-dispatched`, then `complete`, plus the exact failed phase on error.
One 2,700-second aggregate controller deadline caps every external recovery
phase under the 55-minute job limit and leaves an explicit 600-second reserve
for summaries and terminal issue reconciliation. After a tag is
minted, recovery waits for the caller's exact tag-triggered `Release` workflow
and automatically reruns that exact workflow within a strict attempt/deadline bound.
Only that validated workflow may create the GitHub Release; an absent, running,
or terminally failed run cannot be bypassed by recovery. A transient dispatch
failure is retried with bounded backoff and does not misreport an already
verified publish or exact release tag as undone. Dispatch delivery is at-least-once, so
dependency waves serialize on one coalescing branch and resolve stale events to
the newest visible registry version before writing. One retry-wait deadline
covers the complete downstream manifest, and a non-empty manifest fails closed
when no dispatch credential is configured.

Each Kin repository should keep only a thin workflow wrapper and repo-local config.
Callers should pin reusable workflows to a semver tag, for example
`kinlab-ai/kin-actions/.github/workflows/cargo-registry-release.yml@v0.1.38`.

## Full-auto Cargo caller

The release train is notification-independent: every surviving run re-reads the
last immutable tag and all first-parent `main` commits since it. A replaced
pending event therefore cannot lose release intent. Install the combined
template at `.kin-release/release-train-caller.yml` as
`.github/workflows/release-train.yml`; it triggers train work after CI and
recovery after the registry workflow, with scheduled and typed-dispatch
backstops. No human dispatch is required:

```yaml
name: Cargo release train

on:
  workflow_run:
    workflows: [CI, Registry Publish]
    types: [completed]
  repository_dispatch:
    types: [kin-cargo-release-reconcile]
  schedule:
    - cron: "17,47 * * * *"

jobs:
  train:
    if: >-
      github.event_name != 'workflow_run' ||
      (github.event.workflow_run.name == 'CI' &&
       github.event.workflow_run.conclusion == 'success')
    permissions:
      actions: write
      checks: read
      contents: read
      issues: write
      pull-requests: read
    uses: kinlab-ai/kin-actions/.github/workflows/cargo-release-train.yml@vX.Y.Z
    with:
      package: kin-example
      required-workflow: CI
    secrets: inherit

  recovery:
    if: >-
      github.event_name != 'workflow_run' ||
      github.event.workflow_run.name == 'Registry Publish'
    permissions:
      actions: write
      contents: read
      issues: write
    uses: kinlab-ai/kin-actions/.github/workflows/cargo-release-recovery.yml@vX.Y.Z
    with:
      package: kin-example
      required-workflow: CI
      release-workflow: Release
    secrets: inherit
```

The existing registry wrapper then opts into train-owned version authority and
automatic tag minting:

```yaml
jobs:
  release:
    uses: kinlab-ai/kin-actions/.github/workflows/cargo-registry-release.yml@vX.Y.Z
    with:
      package: kin-example
      version-mode: train
      mint-release-tag: true
    secrets: inherit
```

`vX.Y.Z` means the released version containing these workflows; callers must
replace it with an immutable numeric tag. Until the App credentials, protected
environments, auto-merge, and required checks below are configured, the
controllers stop safely instead of weakening admission.

If the caller also consumes Kin crates while its own version is train-owned,
its dependency-wave wrapper must pass `version-mode: train`,
`bump-own-version: false`, and `secrets: inherit`. The general release App then
authors the dependency branch and PR. Train mode rejects the default Actions
token and rejects any attempt to bump the caller's own version.

### Versioned staged activation

Schema 2 binds each controller run, generated PR, landed proof, and Cargo main
proof to one exact finalized version for a one-repository pilot and bottom-up
rollout:

1. Before enabling a caller, enable auto-merge and allow only squash merges
   (`allow_auto_merge=true`, `allow_squash_merge=true`,
   `allow_merge_commit=false`,
   `allow_rebase_merge=false`) with `squash_merge_commit_title=PR_TITLE` and
   `squash_merge_commit_message=PR_BODY`. Require all three exact `main`
   checks, each bound to GitHub Actions App ID `15368`:
   `release / Version bump gate`,
   `release / Registry-only build`, and `release / Repo verification`.
   SHA-pin every external `uses:` in the caller's actual
   `.github/workflows/release.yml`. The scheduled train checks these live
   settings, including strict/up-to-date required-status-check admission, and
   the workflow bytes on every run and refuses mutation if any are absent or
   mutable.
2. In all eight Cargo repositories listed in
   `.kin-release/cargo-train-bootstrap.json`, pin the registry wrapper and new
   `.github/workflows/release-train.yml` to A, pass `secrets: inherit`, enable
   train mode/tag minting, and apply the four recorded
   `bump-own-version: false` dependency-wave changes.
3. Migrate `kin-infra` and `homebrew-kin` from their legacy SHA uses to the
   current stable `vX.Y.Z` tag. Schema 2 has no mutable-ref or SHA exception.
4. Install the dedicated workflow-pin App on exactly the 15 manifest
   repositories, grant Actions, Checks, and Administration read alongside its
   existing pin PR permissions, and remove every bypass actor from the
   version-tag freeze. Administration stays read only and proves live strict
   required-status-check protection for the selected stage before its first
   pin write. GitHub withholds `bypass_actors` from read-only ruleset responses,
   so the released pin controller remains explicitly held until an independently
   managed audit supplies a trusted proof surface. Its separately provisioned
   ruleset-write credential must remain outside candidate-owned workflow code.
5. Add an independently managed required check or protected reviewer policy to
   `kin-actions` itself before releasing this protocol. The candidate-owned
   self-test remains defense in depth, but cannot independently certify a
   coordinated change to its own workflow, guard, and falsifiers.
6. Release the first protocol version, `v0.1.34` or newer. Only `kin-blobs` may
   receive a pin PR until its exact head checks and landed-main four-job proof
   pass. The controller then advances the remaining Cargo callers bottom-up and
   finishes with the seven other consumers. Missing or stale inventory fails
   before any branch, label, PR, or auto-merge write.

## Reusable Workflows

- `.github/workflows/cargo-release-train.yml`
  Reconstructs cumulative trusted drift, prepares the exact Cargo version and
  lockfile bytes, and enables squash auto-merge on the exact protected head.

- `.github/workflows/cargo-registry-release.yml`
  Enforces manual or train-owned version authority, builds without local
  patches, publishes, verifies the exact published version, mints its tag, and
  dispatches downstreams.

- `.github/workflows/cargo-release-recovery.yml`
  Automatically resumes consumer proof, exact tag, bounded exact-tag Release
  validation/rerun, and durable downstream delivery after a Cargo version is
  already present in the registry.

- `.github/workflows/cargo-dependency-wave.yml`
  Handles `kin-registry-release` events and scheduled backstops by updating Cargo registry dependency pins and opening signed-off PRs. Server-created commits use the `github-actions[bot]` identity for truthful automation provenance.

- `.github/workflows/self-release-train.yml`
  Coalesces changes in this repository into an exact `VERSION`, `README.md`, and
  `CONTRIBUTING.md` release PR.

- `.github/workflows/release.yml`
  Converts the exact tested `VERSION` on `main` into one immutable tag and
  finalized GitHub Release, then asks the pin controller to reconcile consumers.

- `.github/workflows/pin-wave.yml`
  Validates the exact consumer inventory and App scope, holds a one-repository
  `kin-blobs` pilot, and advances one proof-gated bottom-up pin PR at a time.

- `.github/workflows/public-history-hygiene.yml`
  Compatibility path for the public metadata safety gate. It blocks private assistant-session references before publication. Internal tracker references are scoped by whether the text becomes a commit message: allowed in commit messages, the PR title, and the PR body, and still rejected in added source content and branch names. The gate is validation-only: it never rewrites Git history, dates, authors, committers, or attribution, and it does not evaluate timestamps or attribution. Tool-specific attribution is optional and is not required by this action. The legacy `check-timestamps` input is accepted and ignored. Consume it from a repository PR workflow:

  ```yaml
  jobs:
    hygiene:
      uses: kinlab-ai/kin-actions/.github/workflows/public-history-hygiene.yml@v0.1.38
  ```

  The pull request body is scanned along with its title. Where a repository sets `squash_merge_commit_message: PR_BODY`, the merge queue mints the squash commit message from that body with nobody at the merge button, so the body is the commit message. Scanning it on `pull_request` reports a violation where it can still be fixed, rather than letting the merge itself write the reference into public history.

- `.github/workflows/scheduled-failure-alarm.yml`
  Gives a scheduled workflow the reader it loses when it comes off pull requests. A failing scheduled run opens one issue for that workflow naming the run and the consecutive-failure count, a streak updates that same issue in place, and the next scheduled success closes it. The count lives in a marker comment inside the issue body rather than in Actions history, because a runs-list lookup decays with run retention and this repository's own guard refuses it. Consume it from the repository whose schedules need watching:

  ```yaml
  name: Scheduled Failure Alarm
  on:
    workflow_run:
      workflows: ["Windows Authority (nightly)"]
      types: [completed]
  permissions:
    issues: write
  jobs:
    alarm:
      uses: kinlab-ai/kin-actions/.github/workflows/scheduled-failure-alarm.yml@v0.1.38
  ```

  The caller names workflows; the shared workflow decides what counts as a scheduled failure, so a consumer cannot wire a filter that never fires. By default only `schedule` runs are covered, so a manual dispatch neither raises the alarm nor clears it: the alarm tracks whether the schedule is healthy, and one green run a human started does not prove that. Set `alarm-events: schedule,workflow_dispatch` where a repository wants a manual green run to close it. A `cancelled` or `skipped` run proves nothing either way and is left alone, which is deliberate: treating a skipped run as success is how a job that skipped two hundred and three times read as green.

## Activation requirements

- `KINLAB_CARGO_TOKEN`
  Required in each Cargo caller's `registry-publish` environment.

- `KIN_CI_BOT_TOKEN`
  Compatibility credential for downstream PR creation and repository dispatch.

- `KIN_RELEASE_BOT_APP_ID` and `KIN_RELEASE_BOT_PRIVATE_KEY`
  Put these in Cargo callers' `registry-publish` environments and this
  repository's `release-tag` environment. Install the App on the current
  repository with Administration read (never write), plus Contents, Pull
  requests, and Issues read/write. Administration read is used only for the
  dedicated strict required-status-check protection readback; the Contents-only
  branch summary omits that invariant. The general release App intentionally
  has no Workflows permission or `main` bypass, and it also has no Actions
  permission. It
  needs narrowly scoped bypasses for its exact `automation/release-next` branch
  and for tag creation, but not for the overlapping tag-freeze rules described
  below.

- `KIN_WORKFLOW_PIN_APP_ID` and `KIN_WORKFLOW_PIN_APP_PRIVATE_KEY`
  Put these only in this repository's `release-followups` environment. This is
  a separate App installed on exactly the repositories in
  `.kin-release/consumers.json`, with Contents, Pull requests, Issues, and
  Workflows read/write plus Actions, Checks, and Administration read. The
  controller verifies the complete installation inventory up front and each
  selected repository's strict App-bound required-check protection before it
  writes that stage.

The external ruleset audit is a release blocker, not a credential to add to this
repository. The auditor must hold its separately provisioned ruleset-write
credential outside `kin-actions`, validate exact ruleset `19932834` including a
visible empty `bypass_actors` array, and publish an independently authenticated
proof that this controller can read without receiving mutation authority. Until
that producer and read-only consumer are provisioned, the pin wave stops before
its first consumer write.

For unattended operation:

1. Enable repository auto-merge and protect `main` with strict/up-to-date
   required status checks, including the exact
   `release / Version bump gate`, `release / Registry-only build`, and
   `release / Repo verification` contexts in addition to the repo's ordinary
   CI. The controller reads this protection and refuses to arm auto-merge if a
   context is missing. Neither App receives a `main` bypass. Give the
   general App the exact `automation/release-next` branch bypass and an exact
   `automation/kin-registry-dependency-wave` branch bypass. If a consumer protects
   automation branches, give the pin App only the exact
   `automation/kin-actions-pin-next` branch bypass there.
2. Split version-tag control into overlapping rulesets: the release App may
   bypass the creation ruleset so it can mint a new tag, while a second freeze
   ruleset blocks tag update, deletion, and non-fast-forward without any
   release-App bypass. Break-glass belongs outside this automatic rollout
   authority.
3. Restrict `registry-publish`, `release-tag`, and `release-followups` to the
   default branch. Do not put required reviewers on an unattended release
   environment.
4. Populate the environment secrets and install each App with only the
   permissions above.
5. Enable auto-merge and allow only squash merges by setting
   `allow_auto_merge=true`, `allow_squash_merge=true`,
   `allow_merge_commit=false`, and `allow_rebase_merge=false`. Set
   `squash_merge_commit_title=PR_TITLE` and
   `squash_merge_commit_message=PR_BODY`; alternate values fail the automatic
   controller's live preflight. Put
   `Kin-Release-Intent: minor` or `Kin-Release-Intent: major` at the end of the
   PR body when escalation is required, and verify the landed first-parent
   commit preserved it.
6. Give both caller jobs `actions: write` and `issues: write`; additionally give
   the train `checks: read` and `pull-requests: read`. Its caller-scoped
   `GITHUB_TOKEN` reads exact-head check provenance and the generated PR, reruns
   only exact failed Actions runs inside the calling repository, and reconciles
   only the package-scoped terminal issue. The general release App still has no
   Actions or Workflows permission.
7. Add the immediate and scheduled caller wrappers, then verify one generated
   PR traverses checks, publish, fresh-consumer proof, tag, GitHub Release, and
   downstream reconciliation.
8. Remove compatibility PATs only after that end-to-end proof.

## Recovery and rollback

The controllers reconcile durable Git, registry, tag, Release, and live-pin
state, so rerunning a failed stage is safe. If a release is bad, publish a fixed
forward version; for Cargo, yank the bad version when appropriate. Never move a
tag, reuse an immutable registry version, force-rewrite a train branch, or edit
consumer history. Pin the fixed version through the same protected wave.

## License

[Apache-2.0](LICENSE).
