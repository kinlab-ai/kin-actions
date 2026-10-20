#!/usr/bin/env python3
"""Kin registry version-bump gate.

Decide whether a PR/commit MUST carry a crate version bump, using clear,
intent-aware rules so that non-releasing chores stop being blocked:

  * Release-affecting changes REQUIRE a version bump:
      - crate/package source trees: ``src/**``, ``crates/**/src/**``,
        ``packages/**/src/**`` and build scripts (``build.rs``)
      - ``Cargo.toml`` dependency / feature changes (the parts of a manifest
        that change what downstream consumers actually build)
  * Non-release changes do NOT require a bump (the gate is skipped):
      - tests / benches / examples, documentation and ``*.md`` files,
        comments, CI config (``.github/**``) and other non-source chores
  * A ``release`` / ``release:*`` PR label (or any changed crate ``src/`` path)
    FORCES the bump requirement, except on the first-party workflow-pin chore
    branch, whose PRs wear those labels for approval routing rather than
    release intent (see :func:`is_pin_chore_pr`).

It also guards release-authority invariants:

  * every version movement must be strictly forward from the Git base under
    full SemVer precedence,
  * a crate version may never move *below* the newest already-published
    version, and
  * a release-affecting change may not land on a version that is already
    published (that would either be a silent no-op or a corruption risk).

Registry authority fails closed: only a 404 means a crate has no published
index; transport, decoding, JSON, and malformed-row failures stop the gate.
The gate classifies the change before it reads the registry, and reads it only
when registry history can change the verdict (see
:func:`registry_read_reasons`). A change that keeps the base version and ships
nothing, such as a CI-only edit, therefore passes while the registry is down,
and every change that does depend on it still fails closed.

The decision logic lives in :func:`evaluate_gate` and the file classifier in
:func:`classify_path`; both are pure so they can be unit-tested without git,
cargo, or network access (see ``scripts/test_check_version_bump.py``).
"""
import argparse
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import kin_registry_index as registry_index
import release_train_policy as train_policy


# --- helpers -------------------------------------------------------------

parse_version = registry_index.parse_version
sparse_index_path = registry_index.sparse_index_path


def run(args, *, text=True, check=True):
    return subprocess.run(args, text=text, check=check, capture_output=True)


def cargo_metadata_version(package):
    data = json.loads(run(["cargo", "metadata", "--no-deps", "--format-version", "1"]).stdout)
    for pkg in data["packages"]:
        if pkg["name"] == package:
            return pkg["version"]
    raise SystemExit(f"package not found in cargo metadata: {package}")


class WorkspaceVersionInherited(ValueError):
    """A member delegates its package version to the workspace root."""


def _manifest_document(text):
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"could not parse manifest TOML: {exc}") from exc
    if not isinstance(document, dict):
        raise SystemExit("manifest root is not a TOML table")
    return document


def _workspace_package_version(document):
    workspace = document.get("workspace")
    if workspace is None:
        return None
    if not isinstance(workspace, dict):
        raise SystemExit("manifest [workspace] is not a table")
    package = workspace.get("package")
    if package is None:
        return None
    if not isinstance(package, dict):
        raise SystemExit("manifest [workspace.package] is not a table")
    version = package.get("version")
    if version is None:
        return None
    if not isinstance(version, str) or not version:
        raise SystemExit("manifest [workspace.package].version is not a string")
    return version


def read_workspace_package_version_text(text):
    """Read only the authoritative ``[workspace.package].version``."""

    version = _workspace_package_version(_manifest_document(text))
    if version is None:
        raise SystemExit("could not read [workspace.package].version from manifest")
    return version


def read_manifest_version_text(text):
    """Read an explicit package version or authoritative workspace version."""

    document = _manifest_document(text)
    package = document.get("package")
    if package is not None:
        if not isinstance(package, dict):
            raise SystemExit("manifest [package] is not a table")
        version = package.get("version")
        if isinstance(version, str) and version:
            return version
        if isinstance(version, dict) and version.get("workspace") is True:
            local_workspace_version = _workspace_package_version(document)
            if local_workspace_version is not None:
                return local_workspace_version
            raise WorkspaceVersionInherited
        raise SystemExit("could not read [package].version from manifest")

    workspace_version = _workspace_package_version(document)
    if workspace_version is not None:
        return workspace_version
    raise SystemExit("could not read version from manifest")


def read_manifest_version(path):
    return read_manifest_version_text(Path(path).read_text(encoding="utf-8"))


def git_show_file(ref, path):
    """Return the text of ``path`` at ``ref`` or ``None`` if it does not exist."""
    result = run(["git", "show", f"{ref}:{path}"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def base_manifest_version(base_ref, manifest):
    text = git_show_file(base_ref, manifest)
    if text is None:
        return None
    try:
        return read_manifest_version_text(text)
    except WorkspaceVersionInherited:
        # Workspace members inherit only from the root's exact
        # [workspace.package].version. Dependency `version` keys are never a
        # package-version fallback.
        root = git_show_file(base_ref, "Cargo.toml")
        if root is None:
            raise SystemExit(
                f"could not resolve inherited base version for {manifest}"
            )
        return read_workspace_package_version_text(root)


def _commit_available(ref):
    return run(["git", "cat-file", "-e", f"{ref}^{{commit}}"], check=False).returncode == 0


def select_base_ref(explicit, push_before, ref_type):
    """Choose the full pushed range while rejecting malformed/missing authority."""
    if explicit:
        return explicit
    if ref_type == "branch" and push_before:
        if push_before == "0" * 40:
            # GitHub uses the all-zero SHA when a branch is created. There is
            # no prior authority in that event, even when the pushed tip has a
            # first parent, so do not silently shrink a multi-commit initial
            # push to HEAD^.
            return ""
        elif not re.fullmatch(r"[0-9a-fA-F]{40}", push_before):
            raise SystemExit(f"invalid push before SHA: {push_before!r}")
        elif not _commit_available(push_before):
            run(
                ["git", "fetch", "--no-tags", "--depth=1", "origin", push_before],
                check=False,
            )
            if not _commit_available(push_before):
                raise SystemExit(
                    f"push before commit {push_before} is unavailable; "
                    "refusing to infer release authority from HEAD^"
                )
        if push_before:
            return push_before
    result = run(["git", "rev-parse", "--verify", "HEAD^"], check=False)
    return "HEAD^" if result.returncode == 0 else ""


def changed_files(base_ref):
    result = run(["git", "diff", "--name-only", f"{base_ref}...HEAD"], check=False)
    if result.returncode != 0:
        result = run(["git", "diff", "--name-only", f"{base_ref}", "HEAD"], check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def published_versions(registry_url, crate_name):
    try:
        records = registry_index.fetch_index(registry_url, crate_name)
    except registry_index.RegistryIndexError as exc:
        raise SystemExit(str(exc)) from exc
    if records is None:
        return []
    # Yanked versions remain published and immutable. They must participate in
    # collision and monotonicity checks even though dependency resolution must
    # not select them as installable latest.
    return [record.version for record in records]


def verify_generated_release(
    *,
    base_ref,
    package,
    manifest,
    base_version,
    target_version,
    generated_paths,
):
    """Require exact bytes from the deterministic Cargo release generator."""

    command = [
        "python3",
        str(Path(__file__).with_name("prepare-cargo-release.py").resolve()),
        "--verify-ref",
        base_ref,
        "--package",
        package,
        "--manifest",
        manifest,
        "--base-version",
        base_version,
        "--target-version",
        target_version,
    ]
    for path in generated_paths:
        command.extend(("--generated-path", path))
    result = run(command, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(
            "deterministic generated-byte verification failed"
            + (f": {detail}" if detail else "")
        )


# --- file classification -------------------------------------------------

# Path segments that mark non-release locations (tests, fixtures, docs) when
# the file is not inside a `src/` tree.
EXCLUDED_DIR_SEGMENTS = {
    "tests", "test", "benches", "bench", "examples", "example", "fuzz",
    "docs", "doc",
}
# Documentation / prose suffixes that never require a release bump.
DOC_SUFFIXES = (".md", ".mdx", ".markdown", ".rst", ".adoc", ".txt")


def _under_any(path, prefixes):
    for prefix in prefixes:
        p = prefix.strip().strip("/")
        if not p:
            continue
        if path == p or path.startswith(p + "/"):
            return True
    return False


def classify_path(path, extra_source_roots=None):
    """Classify a changed path as ``source``, ``manifest`` or ``ignore``.

    ``source``   -> release-affecting crate/package source code.
    ``manifest`` -> a ``Cargo.toml`` (dep/feature relevance checked separately).
    ``ignore``   -> docs, tests, CI and other non-releasing chores.
    """
    norm = (path or "").strip().replace("\\", "/")
    if not norm:
        return "ignore"
    segments = norm.split("/")
    base = segments[-1]

    if base == "Cargo.toml":
        return "manifest"
    # Documentation / prose, regardless of where it lives.
    if base.lower().endswith(DOC_SUFFIXES):
        return "ignore"
    # CI / tooling config is never release source.
    if segments[0] in {".github", ".cargo"}:
        return "ignore"
    # Anything inside a `src/` tree is genuine crate source -- this also covers
    # a crate that happens to be named `test`/`docs` (e.g. crates/test/src/..).
    if "src" in segments:
        return "source"
    # Build scripts live at the crate root, outside `src/`.
    if base == "build.rs":
        return "source"
    # No `src/` tree: conventional test/bench/example/doc locations don't ship.
    if any(seg in EXCLUDED_DIR_SEGMENTS for seg in segments):
        return "ignore"
    # Non-standard source layouts can be declared via --source-path.
    if extra_source_roots and _under_any(norm, extra_source_roots):
        return "source"
    return "ignore"


# --- manifest dependency / feature change detection ----------------------

def manifest_release_signature(text):
    """Return the semantic dependency/feature projection of a manifest.

    TOML dotted keys and table syntax produce the same parsed structure, so
    dependency authority cannot disappear merely because a valid declaration
    is written as ``dependencies.crate = {...}``. Dict equality also ignores
    formatting, comments, and key order while retaining semantic values.
    """

    document = _manifest_document(text)
    projection = {}

    def select_tables(source, keys, *, context):
        selected = {}
        for key in keys:
            if key not in source:
                continue
            value = source[key]
            if not isinstance(value, dict):
                raise SystemExit(
                    f"manifest {context}.{key} is not a TOML table"
                )
            selected[key] = value
        return selected

    projection.update(
        select_tables(
            document,
            ("dependencies", "build-dependencies", "features"),
            context="root",
        )
    )

    workspace = document.get("workspace")
    if workspace is not None:
        if not isinstance(workspace, dict):
            raise SystemExit("manifest [workspace] is not a TOML table")
        selected = select_tables(
            workspace,
            ("dependencies",),
            context="workspace",
        )
        if selected:
            projection["workspace"] = selected

    targets = document.get("target")
    if targets is not None:
        if not isinstance(targets, dict):
            raise SystemExit("manifest [target] is not a TOML table")
        selected_targets = {}
        for target, target_document in targets.items():
            if not isinstance(target_document, dict):
                raise SystemExit(
                    f"manifest target.{target} is not a TOML table"
                )
            selected = select_tables(
                target_document,
                ("dependencies", "build-dependencies"),
                context=f"target.{target}",
            )
            if selected:
                selected_targets[target] = selected
        if selected_targets:
            projection["target"] = selected_targets

    return projection


def manifest_deps_changed(base_text, head_text):
    """True if dependency/feature content of a manifest changed.

    A brand-new manifest (``base_text is None``) is treated as release-relevant.
    A pure version bump or metadata edit returns ``False``.
    """
    if base_text is None:
        return True
    return manifest_release_signature(base_text) != manifest_release_signature(head_text or "")


# --- PR labels -----------------------------------------------------------

def parse_labels(raw):
    if not raw:
        return []
    return [t for t in re.split(r"[,\s]+", raw.strip()) if t]


def has_release_label(labels):
    for label in labels:
        low = label.strip().lower()
        if low == "release" or low.startswith("release:") or low.startswith("release/"):
            return True
    return False


# --- decision ------------------------------------------------------------

def is_pin_chore_pr(*, event_name, base_repo, head_repo, head_branch, pin_branch):
    """True for a first-party workflow-pin chore PR.

    The pin wave labels its PRs ``release:automated`` / ``release:consumer-bump``
    so they route through the same approval automation real release work uses.
    Those labels otherwise force a version bump the wave can never supply: a pin
    PR only rewrites ``.github/workflows/**``, which classifies as ``ignore``,
    so the crate version legitimately stays put and the PR can never merge.

    Head-repo identity is required, so a fork cannot claim the exemption by
    naming its branch after the pin branch.
    """
    if event_name != "pull_request":
        return False
    if not pin_branch or head_branch != pin_branch:
        return False
    if not base_repo or head_repo != base_repo:
        return False
    return True


def registry_read_reasons(*, version_mode, version, base_version, relevant,
                          release_label, pin_chore, event_name,
                          train_authority):
    """Return why registry history can change the verdict, or ``[]``.

    Every registry check compares a version this change publishes, or moves
    to, against immutable history. When the version equals its base, no file
    is release-affecting, no label forces a bump and the run holds no train
    authority, those checks could only judge the base commit's own state:
    nothing here can publish, collide with a published version or fall below
    one. A push publishes only as a release candidate, which needs a forward
    version or no base, so version movement covers every push that could
    release. Train mode gives labels no authority, so they force nothing
    there.
    """
    reasons = []
    if relevant:
        reasons.append(f"{len(relevant)} release-affecting file(s) changed")
    if version_mode == "manual" and release_label and not pin_chore:
        reasons.append("release label set")
    if base_version is None:
        reasons.append("no base version")
    elif version != base_version:
        reasons.append(f"version moved from base {base_version} to {version}")
    if version_mode == "train" and event_name == "controller":
        reasons.append("release controller run")
    if version_mode == "train" and train_authority:
        reasons.append("generated train PR")
    return reasons


def evaluate_gate(*, package, version, base_version, published,
                  source_changes, dep_manifest_changes, release_label,
                  pin_chore=False):
    """Pure gate decision. Returns ``(failures, require_bump, relevant)``.

    ``published`` is ``None`` when the registry was not read because
    :func:`registry_read_reasons` found nothing it could change. A decision
    that still needs registry history then fails closed rather than treating
    the unread registry as empty.
    """
    failures = []
    registry_read = published is not None
    published = published or []
    version_precedence = parse_version(version)
    newest = max(published, key=parse_version) if published else None
    if newest and version_precedence < parse_version(newest):
        failures.append(f"{package}@{version} is lower than newest published {newest}")
    if (
        base_version is not None
        and version != base_version
        and version_precedence <= parse_version(base_version)
    ):
        failures.append(
            f"{package} version must move strictly forward from base "
            f"{base_version}, not to {version}"
        )
    release_candidate = (
        base_version is None
        or version_precedence > parse_version(base_version)
    )
    if release_candidate and version in published:
        failures.append(
            f"{package}@{version} is already published and immutable; "
            "a release candidate must use a new version"
        )

    relevant = list(source_changes) + list(dep_manifest_changes)
    # The exemption suppresses LABEL forcing only. It cannot carry release
    # content, because `bool(relevant)` below still requires the bump for any
    # release-affecting path, exempt PR or not. That term is the whole guard;
    # narrowing this one on `relevant` too would be inert.
    forcing_label = release_label and not pin_chore
    require_bump = bool(relevant) or forcing_label

    if require_bump and base_version is not None:
        if version == base_version:
            reason = "release label set" if (forcing_label and not relevant) else \
                f"{len(relevant)} release-affecting file(s) changed"
            failures.append(
                f"{reason} but {package} version stayed at {version}; "
                "bump major/minor/patch before merging"
            )
    if not registry_read and (require_bump or release_candidate):
        failures.append(
            f"{package} registry history was not read, but this change "
            "depends on it"
        )
    return failures, require_bump, relevant


def is_release_candidate(version, base_version):
    """A commit owns release authority only on an initial or forward version."""
    if base_version is None:
        return True
    return parse_version(version) > parse_version(base_version)


def main():
    parser = argparse.ArgumentParser(description="Kin registry version-bump gate")
    parser.add_argument("--package", required=True)
    parser.add_argument("--manifest", default="Cargo.toml")
    parser.add_argument("--base-ref", default=os.environ.get("BASE_REF", ""))
    parser.add_argument("--registry-url", default=os.environ.get("KINLAB_CARGO_REGISTRY_URL", "https://kinlab.ai"))
    parser.add_argument(
        "--source-path", action="append", default=[],
        help="extra release-source path prefix for non-standard layouts "
             "(standard src/, crates/**/src/, packages/**/src/, build.rs and "
             "Cargo.toml deps are always detected)",
    )
    parser.add_argument(
        "--labels", default=os.environ.get("PR_LABELS", ""),
        help="comma/space separated PR labels; a 'release'/'release:*' label "
             "forces the bump requirement",
    )
    parser.add_argument(
        "--version-mode",
        choices=("manual", "train"),
        default="manual",
        help="manual requires contributors to bump release changes; train "
             "reserves version movement for the generated release branch",
    )
    parser.add_argument(
        "--release-intent",
        choices=("patch", "minor", "major"),
        default="patch",
        help="immutable controller intent in train mode; labels never select it",
    )
    parser.add_argument(
        "--event-name",
        default=os.environ.get("GITHUB_EVENT_NAME", ""),
    )
    parser.add_argument(
        "--ref-type",
        default=os.environ.get("GITHUB_REF_TYPE", ""),
    )
    parser.add_argument(
        "--ref-name",
        default=os.environ.get("GITHUB_REF_NAME", ""),
    )
    parser.add_argument(
        "--default-branch",
        default=os.environ.get("GITHUB_EVENT_REPOSITORY_DEFAULT_BRANCH", ""),
    )
    parser.add_argument("--base-repo", default="")
    parser.add_argument("--head-repo", default="")
    parser.add_argument("--head-branch", default="")
    parser.add_argument("--train-branch", default="automation/release-next")
    parser.add_argument(
        "--pin-branch",
        default="automation/kin-actions-pin-next",
        help="first-party workflow-pin chore branch; its PRs carry release:* "
             "approval labels but no release intent, so those labels do not "
             "force a bump while the change set stays non-release-affecting",
    )
    parser.add_argument(
        "--generated-path",
        action="append",
        default=[],
        help="exact path the automatic train may generate; repeat per path",
    )
    args = parser.parse_args()

    extra_source_roots = args.source_path or []
    version = cargo_metadata_version(args.package)
    try:
        manifest_version = read_manifest_version(args.manifest)
        if version != manifest_version:
            print(f"warning: cargo metadata resolved {args.package}@{version}, "
                  f"manifest has {manifest_version}")
    except (SystemExit, OSError, WorkspaceVersionInherited):
        pass

    base_ref = select_base_ref(
        args.base_ref,
        os.environ.get("PUSH_BEFORE", ""),
        os.environ.get("GITHUB_REF_TYPE", ""),
    )

    changed = changed_files(base_ref) if base_ref else []
    base_version = base_manifest_version(base_ref, args.manifest) if base_ref else None

    source_changes = []
    manifest_changes = []
    for path in changed:
        kind = classify_path(path, extra_source_roots)
        if kind == "source":
            source_changes.append(path)
        elif kind == "manifest":
            manifest_changes.append(path)

    dep_manifest_changes = []
    for manifest in manifest_changes:
        base_text = git_show_file(base_ref, manifest) if base_ref else None
        try:
            head_text = Path(manifest).read_text(encoding="utf-8")
        except OSError:
            head_text = ""
        if manifest_deps_changed(base_text, head_text):
            dep_manifest_changes.append(manifest)

    labels = parse_labels(args.labels)
    release_label = has_release_label(labels)
    pin_chore = is_pin_chore_pr(
        event_name=args.event_name,
        base_repo=args.base_repo,
        head_repo=args.head_repo,
        head_branch=args.head_branch,
        pin_branch=args.pin_branch,
    )

    relevant = list(source_changes) + list(dep_manifest_changes)
    train_authority = (
        args.version_mode == "train"
        and train_policy.trusted_train_pr(
            event_name=args.event_name,
            base_repo=args.base_repo,
            head_repo=args.head_repo,
            head_branch=args.head_branch,
            train_branch=args.train_branch,
            labels=labels,
        )
    )
    registry_reasons = registry_read_reasons(
        version_mode=args.version_mode,
        version=version,
        base_version=base_version,
        relevant=relevant,
        release_label=release_label,
        pin_chore=pin_chore,
        event_name=args.event_name,
        train_authority=train_authority,
    )
    if registry_reasons:
        print("Registry history is required: " + "; ".join(registry_reasons))
        published = published_versions(args.registry_url, args.package)
    else:
        published = None

    if args.version_mode == "train":
        result = train_policy.evaluate_train_gate(
            package=args.package,
            version=version,
            base_version=base_version,
            published=published,
            relevant_paths=relevant,
            changed_paths=changed,
            generated_paths=args.generated_path,
            labels=labels,
            release_intent=args.release_intent,
            event_name=args.event_name,
            ref_type=args.ref_type,
            ref_name=args.ref_name,
            default_branch=args.default_branch,
            base_repo=args.base_repo,
            head_repo=args.head_repo,
            head_branch=args.head_branch,
            train_branch=args.train_branch,
        )
        failures = result["failures"]
        require_bump = result["release_needed"]
        release_candidate = result["release_candidate"]
        release_intent = result["release_intent"]
        trusted_train = result["trusted_train_pr"]
        if release_candidate:
            try:
                verify_generated_release(
                    base_ref=base_ref,
                    package=args.package,
                    manifest=args.manifest,
                    base_version=base_version,
                    target_version=version,
                    generated_paths=args.generated_path,
                )
            except ValueError as exc:
                failures.append(str(exc))
                release_candidate = False
    else:
        failures, require_bump, relevant = evaluate_gate(
            package=args.package,
            version=version,
            base_version=base_version,
            published=published,
            source_changes=source_changes,
            dep_manifest_changes=dep_manifest_changes,
            release_label=release_label,
            pin_chore=pin_chore,
        )
        release_candidate = is_release_candidate(version, base_version)
        release_intent = train_policy.highest_intent(labels)
        trusted_train = False

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as stream:
            stream.write(f"release_candidate={'true' if release_candidate else 'false'}\n")
            stream.write(f"release_needed={'true' if require_bump else 'false'}\n")
            stream.write(f"release_intent={release_intent}\n")
            stream.write(f"trusted_train_pr={'true' if trusted_train else 'false'}\n")

    if published is None:
        newest_report = "<not read>"
    else:
        newest = max(published, key=parse_version) if published else None
        newest_report = newest or "<none>"
    print("Kin version gate")
    print(f"  package           : {args.package}")
    print(f"  version mode      : {args.version_mode}")
    print(f"  version           : {version}")
    print(f"  registry read     : {'yes' if published is not None else 'no'}")
    print(f"  newest published  : {newest_report}")
    print(f"  base ref          : {base_ref or '<none>'}")
    print(f"  base version      : {base_version or '<unknown>'}")
    print(f"  release label     : {'yes' if release_label else 'no'}")
    # Train mode never consults the exemption, so reporting it there would read
    # as "the exemption applied" on a release-authority surface.
    pin_chore_report = "n/a" if args.version_mode == "train" else (
        "yes" if pin_chore else "no"
    )
    print(f"  pin chore PR      : {pin_chore_report}")
    print(f"  source changes    : {len(source_changes)}")
    print(f"  dep manifest chgs : {len(dep_manifest_changes)}")
    print(f"  bump required     : {'yes' if require_bump else 'no'}")
    print(f"  release intent    : {release_intent}")
    print(f"  trusted train PR  : {'yes' if trusted_train else 'no'}")
    print(f"  release candidate : {'yes' if release_candidate else 'no'}")
    for path in relevant[:20]:
        print(f"    - {path}")
    if len(relevant) > 20:
        print(f"    ... {len(relevant) - 20} more")

    if failures:
        print("FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    if not require_bump:
        print("OK: no release-affecting changes; version bump not required.")
        if published is None:
            print(
                "  The registry was not read: the version equals base and "
                "nothing ships, so registry history cannot change this verdict."
            )
    else:
        print("OK: version is acceptable for the changed files and registry state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
