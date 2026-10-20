"""Pure version-authority policy for the automatic Cargo release train."""

from __future__ import annotations

import re
from dataclasses import dataclass


_CORE_IDENTIFIER = r"(?:0|[1-9][0-9]*)"
_PRERELEASE_IDENTIFIER = (
    r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
)
_BUILD_IDENTIFIER = r"[0-9A-Za-z-]+"
SEMVER_RE = re.compile(
    rf"^(?P<major>{_CORE_IDENTIFIER})\."
    rf"(?P<minor>{_CORE_IDENTIFIER})\."
    rf"(?P<patch>{_CORE_IDENTIFIER})"
    rf"(?:-(?P<prerelease>{_PRERELEASE_IDENTIFIER}"
    rf"(?:\.{_PRERELEASE_IDENTIFIER})*))?"
    rf"(?:\+(?P<build>{_BUILD_IDENTIFIER}"
    rf"(?:\.{_BUILD_IDENTIFIER})*))?$"
)
STABLE_RE = re.compile(
    rf"^({_CORE_IDENTIFIER})\.({_CORE_IDENTIFIER})\.({_CORE_IDENTIFIER})$"
)


class TrainPolicyError(ValueError):
    """Train policy input was malformed."""


@dataclass(frozen=True, order=True)
class StableVersion:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, raw: str) -> "StableVersion":
        match = STABLE_RE.fullmatch(raw or "")
        if not match:
            raise TrainPolicyError(
                f"automatic train requires stable numeric SemVer, got {raw!r}"
            )
        return cls(*(int(part) for part in match.groups()))

    def bump(self, intent: str) -> "StableVersion":
        if intent == "patch":
            return StableVersion(self.major, self.minor, self.patch + 1)
        if intent == "minor":
            return StableVersion(self.major, self.minor + 1, 0)
        if intent == "major":
            return StableVersion(self.major + 1, 0, 0)
        raise TrainPolicyError(f"invalid release intent: {intent!r}")

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def semver_precedence(raw: str) -> tuple[object, ...]:
    """Return full SemVer precedence while ignoring build metadata."""

    match = SEMVER_RE.fullmatch(raw or "")
    if match is None:
        raise TrainPolicyError(f"invalid published SemVer: {raw!r}")
    prerelease = match.group("prerelease")
    identifiers: tuple[tuple[int, object], ...] = ()
    if prerelease is not None:
        identifiers = tuple(
            (0, int(identifier))
            if re.fullmatch(r"[0-9]+", identifier)
            else (1, identifier)
            for identifier in prerelease.split(".")
        )
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        prerelease is None,
        identifiers,
    )


def highest_intent(labels: list[str]) -> str:
    normalized = {label.strip().lower() for label in labels}
    if normalized & {"release:major", "release/major"}:
        return "major"
    if normalized & {"release:minor", "release/minor"}:
        return "minor"
    return "patch"


def explicit_release_intent(labels: list[str]) -> bool:
    normalized = {label.strip().lower() for label in labels}
    return bool(
        normalized
        & {
            "release",
            "release:patch",
            "release/patch",
            "release:minor",
            "release/minor",
            "release:major",
            "release/major",
        }
    )


# GitHub's merge queue validates queued content on a branch it owns:
# `gh-readonly-queue/<base branch>/pr-<number>-<base object id>`. Both object-ID
# widths are accepted so a SHA-256 repository is classified rather than rejected.
QUEUE_REF_PREFIX = "gh-readonly-queue"
_OBJECT_ID = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"


def queue_validation_ref(
    ref_type: str, ref_name: str, default_branch: str
) -> bool:
    """Return True for the exact queue ref that validates ``default_branch``.

    The pattern is built from the default branch the queue serves, so a ref
    naming any other branch is not a queue run for this repository. Callers
    treat a False result on a `merge_group` event as a hard failure rather than
    as an ordinary event, because only GitHub mints these refs.
    """

    if ref_type != "branch" or not default_branch:
        return False
    pattern = re.compile(
        re.escape(f"{QUEUE_REF_PREFIX}/{default_branch}")
        + rf"/pr-[1-9][0-9]*-{_OBJECT_ID}"
    )
    return bool(pattern.fullmatch(ref_name or ""))


def trusted_train_pr(
    *,
    event_name: str,
    base_repo: str,
    head_repo: str,
    head_branch: str,
    train_branch: str,
    labels: list[str],
) -> bool:
    normalized = {label.strip().lower() for label in labels}
    return (
        event_name == "pull_request"
        and bool(base_repo)
        and head_repo == base_repo
        and head_branch == train_branch
        and "release:automated" in normalized
    )


def evaluate_train_gate(
    *,
    package: str,
    version: str,
    base_version: str | None,
    published: list[str] | None,
    relevant_paths: list[str],
    changed_paths: list[str],
    generated_paths: list[str],
    labels: list[str],
    release_intent: str,
    event_name: str,
    ref_type: str,
    ref_name: str,
    default_branch: str,
    base_repo: str,
    head_repo: str,
    head_branch: str,
    train_branch: str,
) -> dict[str, object]:
    """Evaluate train ownership without relying on mutable PR metadata alone.

    ``published`` is ``None`` when the caller did not read the registry because
    its history could not change the verdict. Any run that still needs that
    history, because it moves the version, needs a release or holds train
    authority, fails closed rather than treating the unread registry as empty.
    """

    failures: list[str] = []
    registry_read = published is not None
    if published is None:
        published = []
    current = StableVersion.parse(version)
    if base_version is None:
        failures.append("automatic train requires an established Git base version")
        base = None
    else:
        base = StableVersion.parse(base_version)
        if registry_read and str(base) not in published:
            failures.append(
                f"automatic train base {package}@{base} is absent from "
                "immutable registry history"
            )

    # The strict shared registry reader supplies every immutable row, including
    # yanked versions. Automatic targets are stable, but historical prereleases
    # still constrain monotonicity under full SemVer precedence.
    published_precedence = [
        (semver_precedence(raw), raw) for raw in published
    ]
    current_precedence = semver_precedence(str(current))
    newest_published = (
        max(published_precedence, key=lambda item: item[0])
        if published_precedence
        else None
    )
    if newest_published and current_precedence < newest_published[0]:
        failures.append(
            f"{package}@{version} is lower than newest published "
            f"{newest_published[1]}"
        )

    if release_intent not in {"patch", "minor", "major"}:
        raise TrainPolicyError(
            f"invalid immutable release intent: {release_intent!r}"
        )
    intent = release_intent
    # Mutable labels have zero authority over whether or how a train releases.
    # They remain only as authentication metadata for the generated PR branch.
    release_needed = bool(relevant_paths)
    release_candidate = False
    generated = set(generated_paths)
    changed = set(changed_paths)
    if not generated:
        failures.append("automatic train generated-path allowlist is empty")

    # Tag delivery is a release-side effect of the already-admitted main
    # version. It never re-enters Cargo publication authority.
    if event_name == "push" and ref_type == "tag":
        release_needed = False
    elif event_name == "controller":
        if base is not None and current != base:
            failures.append(
                f"release controller expected current version {base}, got {current}"
            )
    elif trusted_train_pr(
        event_name=event_name,
        base_repo=base_repo,
        head_repo=head_repo,
        head_branch=head_branch,
        train_branch=train_branch,
        labels=labels,
    ):
        if base is not None:
            matches = [
                level
                for level in ("patch", "minor", "major")
                if current == base.bump(level)
            ]
            if len(matches) != 1:
                failures.append(
                    f"generated train must move {base} to one exact automatic "
                    f"successor, not {current}"
                )
            else:
                # The committed generated version is immutable evidence of the
                # controller's decision. A mutable release label can neither
                # downgrade nor escalate it after the PR is opened.
                intent = matches[0]
        extras = sorted(changed - generated)
        if extras:
            failures.append(
                "generated train changed non-generated paths: "
                + ", ".join(extras)
            )
        if not changed:
            failures.append("generated train PR has no generated changes")
        if newest_published and current_precedence <= newest_published[0]:
            failures.append(
                f"{package}@{version} is not newer than immutable registry "
                f"history through {newest_published[1]}; generated train must "
                "choose a fresh version"
            )
        release_candidate = not failures
        release_needed = True
    elif event_name == "pull_request":
        if base is not None and current != base:
            failures.append(
                "manual version edits are forbidden in train mode; "
                f"{train_branch} owns the version"
            )
    elif event_name == "push":
        if ref_type != "branch" or not default_branch or ref_name != default_branch:
            failures.append(
                "version-moving push authority requires the exact default "
                f"branch {default_branch!r}, got {ref_type}:{ref_name}"
            )
        if base is not None and current != base:
            allowed_targets = {
                base.bump(level) for level in ("patch", "minor", "major")
            }
            if current not in allowed_targets:
                failures.append(
                    f"version-moving main push must be one automatic successor "
                    f"of {base}, got {current}"
                )
            else:
                intent = next(
                    level
                    for level in ("patch", "minor", "major")
                    if current == base.bump(level)
                )
            extras = sorted(changed - generated)
            if extras:
                failures.append(
                    "version-moving main push changed non-generated paths: "
                    + ", ".join(extras)
                )
            if not changed:
                failures.append("version-moving main push has no generated changes")
            if newest_published and current_precedence <= newest_published[0]:
                failures.append(
                    f"{package}@{version} is not newer than immutable registry "
                    f"history through {newest_published[1]}; release authority "
                    "is already consumed"
                )
            release_candidate = not failures
            release_needed = False
    elif event_name == "merge_group":
        # A merge-queue run validates the queued group against the branch tip.
        # Its range spans every pull request in the group and its payload names
        # none of them, so it can hold no version authority: the pull-request
        # run judged the exact change and the default-branch push judges what
        # actually lands. Granting authority here is impossible; refusing to
        # classify the event would eject every queued pull request instead.
        if not queue_validation_ref(ref_type, ref_name, default_branch):
            failures.append(
                "merge_group must run on the exact "
                f"{default_branch or '<unset>'} merge-queue ref, got "
                f"{ref_type}:{ref_name}"
            )
    else:
        failures.append(
            f"train mode does not grant version authority to event {event_name!r}"
        )

    is_train_pr = trusted_train_pr(
        event_name=event_name,
        base_repo=base_repo,
        head_repo=head_repo,
        head_branch=head_branch,
        train_branch=train_branch,
        labels=labels,
    )
    # Mirrors the caller's registry_read_reasons for train mode. Every path that
    # sets release_needed or release_candidate falls under one of these terms.
    if not registry_read and (
        bool(relevant_paths)
        or base is None
        or current != base
        or event_name == "controller"
        or is_train_pr
    ):
        failures.append(
            f"{package} registry history was not read, but this run depends on it"
        )
        release_candidate = False

    return {
        "failures": failures,
        "release_needed": release_needed,
        "release_candidate": release_candidate,
        "release_intent": intent,
        "trusted_train_pr": is_train_pr,
    }
