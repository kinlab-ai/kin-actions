# Contributing to kin-actions

Thanks for your interest in kin-actions. This guide covers how to work with the
reusable workflows, the conventions this repository follows, and how to get
changes reviewed.

## Development Setup

kin-actions contains GitHub Actions reusable workflows — there is no compiled
artifact. To iterate locally, use [act](https://github.com/nektos/act) to run
workflows against a local Docker runtime, or test against a fork.

Before opening a pull request:

- Lint workflow YAML with `actionlint` if available.
- Verify that any changed workflow can be consumed by at least one downstream
  caller in a fork/test environment.

## Versioning and Callers

Callers pin kin-actions to an immutable semver tag. Do not edit `VERSION` in an
ordinary contribution: the protected self-release train owns `VERSION` and the
exact documentation pins, then creates the tag only after required checks pass.
Source drift defaults to a patch. When compatibility requires escalation, end
the PR body (and therefore the squash commit body) with exactly one immutable
trailer:

```
Kin-Release-Intent: minor
```

Use `major` for a major release. The repository must allow squash merges only
(`allow_squash_merge=true`, `allow_merge_commit=false`,
`allow_rebase_merge=false`) and set `squash_merge_commit_title=PR_TITLE` plus
`squash_merge_commit_message=PR_BODY`. Alternate strategies do not preserve
this contract and the automatic train refuses to run under them. Verify the
landed first-parent commit retained the trailer. Mutable PR labels do not
select automatic release intent.

Callers reference workflows like:

```yaml
uses: kinlab-ai/kin-actions/.github/workflows/cargo-registry-release.yml@v0.1.37
```

## DCO Sign-Off

This project uses the [Developer Certificate of Origin
(DCO)](https://developercertificate.org/). Every commit you push on a pull
request must carry a `Signed-off-by` trailer:

```
Signed-off-by: Your Name <you@example.com>
```

Add it by passing `-s` to `git commit`:

```sh
git commit -s -m "fix(metadata): reject private session URLs"
```

If you forgot to sign off earlier commits on your branch:

```sh
git commit -s --amend              # amend only the last commit
git rebase --signoff HEAD~N        # add sign-off to the last N commits
```

By signing off you certify that you wrote the code (or have the right to
submit it) and that it may be distributed under the Apache License 2.0 that
governs this repository. Bot-authored commits (Dependabot, GitHub Actions)
are exempt.

## AI-Assisted Contributions

Kin is built with significant AI assistance, and we welcome AI-assisted
contributions from the community. A few requirements:

- **You are responsible for AI-generated code you submit.** Review every
  line before opening a PR. If the model hallucinated a workflow step, an
  insecure secret reference, or a broken job dependency, that is your bug to
  catch.
- **AI-generated code is your contribution.** By signing off your commits
  you assert that you have reviewed the generated code and are submitting it
  under your own name.
- **Do not publish private agent context.** Keep session links, session IDs,
  and unreviewed model transcripts out of commit messages and comments.
- **Tool-specific attribution is optional.** The gate does not require it.
  Existing authorship or attribution must not be stripped or rewritten.
  Review and sign off your contribution under the ordinary DCO process.

## Commit Messages

This repository uses [Conventional Commits](https://www.conventionalcommits.org/).
A `type(scope): summary` subject is the expected shape:

```
fix(metadata): protect private session references
feat(release): add optional dry-run input to cargo-registry-release
chore(version): bump to v0.1.6
```

Common types are `feat`, `fix`, `docs`, `test`, `refactor`, and `chore`. Write
the summary in the imperative mood and keep it focused on what changed and why.

## Branch Naming and Commit Hygiene

Public Git history is part of the product, so keep it clean and reviewable:

- **Keep branch names topical, not tracker-coded.** Prefer short, descriptive
  names like `fix/bot-exemption` or `feat/dry-run`. Avoid embedding internal
  issue or tracker IDs in a branch name.
- **Write durable subjects and bodies.** Commit messages should describe the
  technical change and why it was made. Keep internal tracker IDs, session
  identifiers, and private links out of public commit metadata.
- **Keep hooks validation-only.** Hooks may reject private references or
  secrets, but must not rewrite timestamps, authors, committers, attribution,
  or existing history. Do not bypass a failing safety check with `--no-verify`.

## Pull Requests

- **Keep PRs scoped.** Stage only the files your change actually needs.
  Unrelated cleanups belong in their own PR.
- If a change is breaking (removes or renames inputs/outputs/secrets), add the
  appropriate immutable `Kin-Release-Intent` trailer and describe the
  compatibility impact. The automatic train, not the feature branch, updates
  `VERSION`.

## Reporting Issues

File issues on [kinlab-ai/kin-actions](https://github.com/kinlab-ai/kin-actions/issues).

For security vulnerabilities, do **not** open a public issue. Follow the
private reporting process in [SECURITY.md](SECURITY.md).

Triage SLA: security issues are acknowledged within 48 hours; general issues
within 7 days.

## Repository Boundaries

kin-actions provides the CI/CD substrate shared by all Kin repositories. It has
no product logic — it enforces build hygiene, publication, and dependency
propagation. Semantic, graph, or inference changes belong in the owning crates
(`kin`, `kin-db`, `kin-infer`, and so on).

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the license that covers this repository.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By
participating, you are expected to uphold it.
