#!/usr/bin/env python3
"""Unit tests for the Kin registry version-bump gate.

Run with: ``python3 -m unittest discover -s scripts -p 'test_*.py'``

These exercise the pure classifier (:func:`classify_path`), the manifest
dependency-change detector (:func:`manifest_deps_changed`) and the gate
decision (:func:`evaluate_gate`) with no git / cargo / network access. The two
headline cases the gate must get right are covered explicitly:

  * a docs-only PR passes WITHOUT a version bump
    (``EvaluateGate.test_docs_only_pr_passes_without_bump``), and
  * a crate-``src`` PR still REQUIRES one
    (``EvaluateGate.test_crate_src_change_without_bump_fails``).
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest import mock


def _load(name, filename):
    path = Path(__file__).resolve().parent / filename
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cvb = _load("check_version_bump", "check-version-bump.py")
CHECKSUM = "0" * 64
NON_ASCII_NUMERIC_VERSIONS = ("1.2٢.3", "1.2.3-1٢")


def registry_row(
    *,
    name="x",
    version="1.0.0",
    yanked=False,
    checksum=CHECKSUM,
    omit=(),
):
    row = {
        "name": name,
        "vers": version,
        "yanked": yanked,
        "cksum": checksum,
        "deps": [],
        "features": {},
    }
    for key in omit:
        row.pop(key)
    return (json.dumps(row, separators=(",", ":")) + "\n").encode()


class ClassifyPath(unittest.TestCase):
    def c(self, path):
        return cvb.classify_path(path, [])

    def test_release_source_paths(self):
        for path in [
            "src/main.rs",
            "src/lib.rs",
            "crates/foo/src/lib.rs",
            "crates/foo/src/nested/mod.rs",
            "packages/bar/src/index.rs",
            "build.rs",
            "crates/foo/build.rs",
        ]:
            self.assertEqual(self.c(path), "source", path)

    def test_manifest_paths(self):
        self.assertEqual(self.c("Cargo.toml"), "manifest")
        self.assertEqual(self.c("crates/foo/Cargo.toml"), "manifest")

    def test_non_release_paths_are_ignored(self):
        for path in [
            "README.md",
            "docs/guide.md",
            "crates/foo/README.md",
            "crates/foo/CHANGELOG.md",
            ".github/workflows/ci.yml",
            ".cargo/config.toml",
            "crates/foo/tests/integration.rs",
            "tests/it.rs",
            "benches/bench.rs",
            "examples/demo.rs",
            "fuzz/fuzz_targets/a.rs",
            ".gitignore",
            "LICENSE",
        ]:
            self.assertEqual(self.c(path), "ignore", path)

    def test_crate_named_test_under_src_is_still_source(self):
        # A crate literally named `test`/`docs` must not be mistaken for a
        # test/doc directory when it has a real src tree.
        self.assertEqual(self.c("crates/test/src/lib.rs"), "source")
        self.assertEqual(self.c("crates/docs/src/lib.rs"), "source")

    def test_markdown_under_src_is_ignored(self):
        self.assertEqual(self.c("crates/foo/src/README.md"), "ignore")

    def test_extra_source_root(self):
        self.assertEqual(cvb.classify_path("runtime/engine.rs", ["runtime"]), "source")
        self.assertEqual(cvb.classify_path("runtime/notes.md", ["runtime"]), "ignore")


class ManifestDepsChanged(unittest.TestCase):
    def test_dependency_value_change_detected(self):
        base = '[package]\nname = "x"\nversion = "0.1.0"\n[dependencies]\nserde = "1.0"\n'
        head = '[package]\nname = "x"\nversion = "0.1.0"\n[dependencies]\nserde = "1.1"\n'
        self.assertTrue(cvb.manifest_deps_changed(base, head))

    def test_version_only_change_is_not_a_dep_change(self):
        base = '[package]\nname = "x"\nversion = "0.1.0"\n[dependencies]\nserde = "1.0"\n'
        head = '[package]\nname = "x"\nversion = "0.2.0"\n[dependencies]\nserde = "1.0"\n'
        self.assertFalse(cvb.manifest_deps_changed(base, head))

    def test_metadata_change_is_not_a_dep_change(self):
        base = '[package]\nname = "x"\ndescription = "a"\n[dependencies]\nserde = "1.0"\n'
        head = '[package]\nname = "x"\ndescription = "b"\n[dependencies]\nserde = "1.0"\n'
        self.assertFalse(cvb.manifest_deps_changed(base, head))

    def test_dev_dependencies_change_is_ignored(self):
        base = '[dependencies]\nserde = "1.0"\n[dev-dependencies]\ntempfile = "3"\n'
        head = '[dependencies]\nserde = "1.0"\n[dev-dependencies]\ntempfile = "4"\n'
        self.assertFalse(cvb.manifest_deps_changed(base, head))

    def test_feature_change_detected(self):
        base = '[features]\ndefault = []\n'
        head = '[features]\ndefault = ["x"]\n'
        self.assertTrue(cvb.manifest_deps_changed(base, head))

    def test_target_dependency_change_detected(self):
        base = '[target.\'cfg(unix)\'.dependencies]\nlibc = "0.2"\n'
        head = '[target.\'cfg(unix)\'.dependencies]\nlibc = "0.3"\n'
        self.assertTrue(cvb.manifest_deps_changed(base, head))

    def test_root_dotted_dependency_change_detected(self):
        base = (
            'dependencies.kin-model = '
            '{ version = "=1.2.2", registry = "kin" }\n'
        )
        head = (
            'dependencies.kin-model = '
            '{ version = "=1.2.3", registry = "kin" }\n'
        )
        self.assertTrue(cvb.manifest_deps_changed(base, head))

    def test_root_dotted_and_table_forms_have_same_signature(self):
        dotted = (
            'dependencies.kin-model = '
            '{ version = "=1.2.3", registry = "kin" }\n'
        )
        table = (
            "[dependencies]\n"
            'kin-model = { registry = "kin", version = "=1.2.3" }\n'
        )
        self.assertFalse(cvb.manifest_deps_changed(dotted, table))

    def test_workspace_dependency_change_detected(self):
        base = (
            "[workspace.dependencies]\n"
            'kin-model = { version = "=1.2.2", registry = "kin" }\n'
        )
        head = (
            "[workspace.dependencies]\n"
            'kin-model = { version = "=1.2.3", registry = "kin" }\n'
        )
        self.assertTrue(cvb.manifest_deps_changed(base, head))

    def test_workspace_inherited_dependency_change_detected(self):
        base = (
            "[dependencies]\n"
            'kin-model = { workspace = true, features = ["old"] }\n'
        )
        head = (
            "[dependencies]\n"
            'kin-model = { workspace = true, features = ["new"] }\n'
        )
        self.assertTrue(cvb.manifest_deps_changed(base, head))

    def test_dotted_dev_dependency_change_is_ignored(self):
        base = 'dev-dependencies.tempfile.version = "3"\n'
        head = 'dev-dependencies.tempfile.version = "4"\n'
        self.assertFalse(cvb.manifest_deps_changed(base, head))

    def test_comment_only_change_is_ignored(self):
        base = '[dependencies]\nserde = "1.0"\n'
        head = '[dependencies]\n# pin to 1.0 for MSRV\nserde = "1.0"\n'
        self.assertFalse(cvb.manifest_deps_changed(base, head))

    def test_new_manifest_is_release_relevant(self):
        self.assertTrue(cvb.manifest_deps_changed(None, '[dependencies]\nserde = "1"\n'))


class SharedSemVer(unittest.TestCase):
    def test_direct_parser_rejects_non_ascii_numeric_identifiers(self):
        for version in NON_ASCII_NUMERIC_VERSIONS:
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "invalid SemVer"):
                    cvb.registry_index.parse_version(version)

    def test_release_gate_rejects_non_ascii_numeric_identifiers(self):
        for version in NON_ASCII_NUMERIC_VERSIONS:
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "invalid SemVer"):
                    cvb.evaluate_gate(
                        package="x",
                        version=version,
                        base_version=None,
                        published=[],
                        source_changes=[],
                        dep_manifest_changes=[],
                        release_label=False,
                    )

    def test_every_release_consumer_uses_ascii_shared_authority(self):
        scripts = Path(__file__).resolve().parent
        sources = {
            name: (scripts / name).read_text(encoding="utf-8")
            for name in (
                "kin_registry_index.py",
                "check-version-bump.py",
                "update-cargo-registry-deps.py",
                "bump-own-version.py",
                "mint-release-tag.sh",
                "publish-crate.sh",
            )
        }
        self.assertIs(cvb.parse_version, cvb.registry_index.parse_version)
        self.assertNotIn(r"\d", sources["kin_registry_index.py"])
        self.assertNotIn(r"\d", sources["update-cargo-registry-deps.py"])
        self.assertIn(
            "parse_version = registry_index.parse_version",
            sources["check-version-bump.py"],
        )
        self.assertIn(
            "return registry_index.parse_version(version)",
            sources["update-cargo-registry-deps.py"],
        )
        self.assertNotIn("SEMVER_RE", sources["bump-own-version.py"])
        self.assertIn(
            "registry_index.parse_version(version)",
            sources["bump-own-version.py"],
        )
        self.assertIn(
            "from kin_registry_index import parse_version",
            sources["mint-release-tag.sh"],
        )
        self.assertIn(
            "from kin_registry_index import parse_index",
            sources["publish-crate.sh"],
        )


class EvaluateGate(unittest.TestCase):
    def gate(self, **kwargs):
        defaults = dict(
            package="x",
            version="0.1.0",
            base_version="0.1.0",
            published=["0.1.0"],
            source_changes=[],
            dep_manifest_changes=[],
            release_label=False,
        )
        defaults.update(kwargs)
        return cvb.evaluate_gate(**defaults)

    def test_docs_only_pr_passes_without_bump(self):
        failures, require_bump, relevant = self.gate()
        self.assertFalse(require_bump)
        self.assertEqual(failures, [])
        self.assertEqual(relevant, [])

    def test_unread_registry_passes_a_change_that_ships_nothing(self):
        failures, require_bump, _ = self.gate(published=None)
        self.assertFalse(require_bump)
        self.assertEqual(failures, [])

    def test_unread_registry_fails_closed_for_a_release_change(self):
        failures, require_bump, _ = self.gate(
            published=None,
            version="0.2.0",
            source_changes=["crates/x/src/lib.rs"],
        )
        self.assertTrue(require_bump)
        self.assertTrue(any("was not read" in m for m in failures))

    def test_unread_registry_fails_closed_for_a_version_move(self):
        failures, _, _ = self.gate(published=None, version="0.1.1")
        self.assertTrue(any("was not read" in m for m in failures))

    def test_crate_src_change_without_bump_fails(self):
        failures, require_bump, _ = self.gate(source_changes=["crates/x/src/lib.rs"])
        self.assertTrue(require_bump)
        self.assertTrue(any("stayed at" in m for m in failures))

    def test_crate_src_change_with_bump_passes(self):
        failures, require_bump, _ = self.gate(
            version="0.2.0", source_changes=["crates/x/src/lib.rs"]
        )
        self.assertTrue(require_bump)
        self.assertEqual(failures, [])

    def test_dep_manifest_change_without_bump_fails(self):
        failures, require_bump, _ = self.gate(dep_manifest_changes=["Cargo.toml"])
        self.assertTrue(require_bump)
        self.assertTrue(failures)

    def test_release_label_forces_bump(self):
        failures, require_bump, _ = self.gate(release_label=True)
        self.assertTrue(require_bump)
        self.assertTrue(any("release label" in m for m in failures))

    def test_pin_chore_pr_is_exempt_from_label_forcing(self):
        failures, require_bump, relevant = self.gate(
            release_label=True, pin_chore=True
        )
        self.assertFalse(require_bump)
        self.assertEqual(failures, [])
        self.assertEqual(relevant, [])

    def test_pin_chore_pr_with_src_change_still_requires_bump(self):
        failures, require_bump, _ = self.gate(
            release_label=True,
            pin_chore=True,
            source_changes=["src/lib.rs"],
        )
        self.assertTrue(require_bump)
        self.assertTrue(any("stayed at" in m for m in failures))

    def test_pin_chore_pr_with_dep_manifest_change_still_requires_bump(self):
        failures, require_bump, _ = self.gate(
            release_label=True,
            pin_chore=True,
            dep_manifest_changes=["Cargo.toml"],
        )
        self.assertTrue(require_bump)
        self.assertTrue(failures)

    def test_already_published_version_with_src_change_fails(self):
        failures, _, _ = self.gate(
            version="0.2.0",
            published=["0.1.0", "0.2.0"],
            source_changes=["src/lib.rs"],
        )
        self.assertTrue(any("already published" in m for m in failures))

    def test_version_only_move_to_published_version_fails(self):
        failures, require_bump, _ = self.gate(
            version="1.1.0",
            base_version="1.0.0",
            published=["1.1.0"],
        )
        self.assertFalse(require_bump)
        self.assertTrue(any("already published and immutable" in m for m in failures))

    def test_initial_version_colliding_with_registry_fails(self):
        failures, _, _ = self.gate(
            version="1.1.0",
            base_version=None,
            published=["1.1.0"],
        )
        self.assertTrue(any("already published and immutable" in m for m in failures))

    def test_yanked_version_remains_an_immutable_collision(self):
        failures, _, _ = self.gate(
            version="1.1.0",
            base_version="1.0.0",
            published=["1.1.0"],
        )
        self.assertTrue(any("already published and immutable" in m for m in failures))

    def test_highest_yanked_version_still_sets_monotonic_floor(self):
        failures, _, _ = self.gate(
            version="1.5.0",
            base_version="1.0.0",
            published=["2.0.0"],
        )
        self.assertTrue(any("lower than newest published 2.0.0" in m for m in failures))

    def test_version_below_newest_published_always_fails(self):
        failures, _, _ = self.gate(version="0.1.0", published=["0.2.0"])
        self.assertTrue(any("lower than newest published" in m for m in failures))

    def test_version_movement_must_be_strictly_forward_from_base(self):
        failures, _, _ = self.gate(
            version="0.1.0",
            base_version="0.2.0",
            published=[],
        )
        self.assertTrue(any("strictly forward from base" in m for m in failures))

    def test_version_only_downgrade_fails(self):
        failures, require_bump, _ = self.gate(
            version="1.9.9",
            base_version="2.0.0",
            published=[],
        )
        self.assertFalse(require_bump)
        self.assertTrue(any("strictly forward from base" in m for m in failures))

    def test_unpublished_higher_base_still_prevents_downgrade(self):
        failures, _, _ = self.gate(
            version="1.5.0",
            base_version="2.0.0",
            published=["1.0.0"],
            source_changes=["src/lib.rs"],
        )
        self.assertTrue(any("strictly forward from base" in m for m in failures))

    def test_stable_to_same_core_prerelease_is_not_forward(self):
        failures, _, _ = self.gate(
            version="1.0.0-rc.1",
            base_version="1.0.0",
            published=[],
            source_changes=["src/lib.rs"],
        )
        self.assertTrue(any("strictly forward from base" in m for m in failures))

    def test_prerelease_to_stable_is_forward(self):
        failures, _, _ = self.gate(
            version="1.0.0",
            base_version="1.0.0-rc.1",
            published=[],
            source_changes=["src/lib.rs"],
        )
        self.assertEqual(failures, [])

    def test_prerelease_identifiers_follow_semver_order(self):
        ordered = (
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        )
        self.assertEqual(sorted(ordered, key=cvb.parse_version), list(ordered))

    def test_build_metadata_only_change_is_not_forward(self):
        failures, _, _ = self.gate(
            version="1.0.0+build.2",
            base_version="1.0.0+build.1",
            published=[],
        )
        self.assertTrue(any("strictly forward from base" in m for m in failures))

    def test_no_base_version_skips_bump_comparison(self):
        # First commit / unresolved base: cannot compare, must not crash.
        failures, require_bump, _ = self.gate(
            base_version=None, source_changes=["src/lib.rs"], published=[]
        )
        self.assertTrue(require_bump)
        self.assertEqual(failures, [])


class PinChorePr(unittest.TestCase):
    def detect(self, **kwargs):
        defaults = dict(
            event_name="pull_request",
            base_repo="kinlab-ai/kin-blobs",
            head_repo="kinlab-ai/kin-blobs",
            head_branch="automation/kin-actions-pin-next",
            pin_branch="automation/kin-actions-pin-next",
        )
        defaults.update(kwargs)
        return cvb.is_pin_chore_pr(**defaults)

    def test_first_party_pin_branch_pr_detected(self):
        self.assertTrue(self.detect())

    def test_fork_head_cannot_claim_the_exemption(self):
        self.assertFalse(self.detect(head_repo="attacker/kin-blobs"))

    def test_other_branch_is_not_a_pin_chore(self):
        self.assertFalse(self.detect(head_branch="automation/release-next"))

    def test_non_pull_request_event_is_not_a_pin_chore(self):
        self.assertFalse(self.detect(event_name="push"))

    def test_empty_pin_branch_disables_the_exemption(self):
        self.assertFalse(self.detect(pin_branch=""))

    def test_empty_base_repo_disables_the_exemption(self):
        self.assertFalse(self.detect(base_repo="", head_repo=""))


class Labels(unittest.TestCase):
    def test_release_labels_detected(self):
        self.assertTrue(cvb.has_release_label(cvb.parse_labels("chore, release")))
        self.assertTrue(cvb.has_release_label(cvb.parse_labels("release:minor")))
        self.assertTrue(cvb.has_release_label(cvb.parse_labels("release/patch")))

    def test_non_release_labels_ignored(self):
        self.assertFalse(cvb.has_release_label(cvb.parse_labels("chore docs tests")))
        self.assertFalse(cvb.has_release_label(cvb.parse_labels("")))


class ReleaseCandidate(unittest.TestCase):
    def test_docs_only_followup_with_same_version_is_not_a_release(self):
        self.assertFalse(cvb.is_release_candidate("1.2.3", "1.2.3"))

    def test_version_movement_owns_release_authority(self):
        self.assertTrue(cvb.is_release_candidate("1.2.4", "1.2.3"))

    def test_downgrade_does_not_own_release_authority(self):
        self.assertFalse(cvb.is_release_candidate("1.2.2", "1.2.3"))

    def test_stable_to_prerelease_does_not_own_release_authority(self):
        self.assertFalse(cvb.is_release_candidate("1.2.3-rc.1", "1.2.3"))

    def test_prerelease_to_stable_owns_release_authority(self):
        self.assertTrue(cvb.is_release_candidate("1.2.3", "1.2.3-rc.1"))

    def test_build_metadata_only_change_does_not_own_release_authority(self):
        self.assertFalse(
            cvb.is_release_candidate("1.2.3+build.2", "1.2.3+build.1")
        )

    def test_first_commit_is_a_release_candidate(self):
        self.assertTrue(cvb.is_release_candidate("1.2.3", None))

    def test_inherited_base_version_resolves_from_workspace_root(self):
        member = (
            '[package]\nname = "x"\nversion.workspace = true\n'
            '[dependencies.dep]\nversion = "9.9.9"\n'
        )
        root = '[workspace.package]\nversion = "1.2.3"\n'

        def show(_ref, path):
            return root if path == "Cargo.toml" else member

        original = cvb.git_show_file
        try:
            cvb.git_show_file = show
            self.assertEqual(
                cvb.base_manifest_version("HEAD^", "crates/x/Cargo.toml"),
                "1.2.3",
            )
        finally:
            cvb.git_show_file = original

    def test_inherited_base_never_falls_back_to_dependency_version(self):
        member = (
            '[package]\nname = "x"\nversion.workspace = true\n'
            '[dependencies.dep]\nversion = "9.9.9"\n'
        )

        def show(_ref, path):
            return '[workspace]\nmembers = ["crates/x"]\n' if path == "Cargo.toml" else member

        with mock.patch.object(cvb, "git_show_file", side_effect=show):
            with self.assertRaisesRegex(
                SystemExit, "workspace.package.*version"
            ):
                cvb.base_manifest_version("HEAD^", "crates/x/Cargo.toml")

    def test_multi_commit_branch_push_uses_event_before(self):
        before = "a" * 40
        with mock.patch.object(cvb, "_commit_available", return_value=True):
            self.assertEqual(cvb.select_base_ref("", before, "branch"), before)

    def test_tag_push_does_not_trust_event_before(self):
        before = "a" * 40
        with mock.patch.object(cvb, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(cvb.select_base_ref("", before, "tag"), "HEAD^")

    def test_zero_before_sha_uses_no_base_even_when_head_has_parent(self):
        with mock.patch.object(cvb, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(cvb.select_base_ref("", "0" * 40, "branch"), "")
        run.assert_not_called()

    def test_malformed_before_sha_fails_closed(self):
        with self.assertRaisesRegex(SystemExit, "invalid push before SHA"):
            cvb.select_base_ref("", "not-a-sha", "branch")

    def test_missing_before_commit_fails_closed_after_fetch(self):
        before = "a" * 40
        with mock.patch.object(cvb, "_commit_available", return_value=False), mock.patch.object(
            cvb, "run"
        ) as run:
            with self.assertRaisesRegex(SystemExit, "refusing to infer"):
                cvb.select_base_ref("", before, "branch")
        run.assert_called_once_with(
            ["git", "fetch", "--no-tags", "--depth=1", "origin", before],
            check=False,
        )

    def test_missing_before_commit_is_recovered_by_fetch(self):
        before = "a" * 40
        with mock.patch.object(
            cvb, "_commit_available", side_effect=[False, True]
        ), mock.patch.object(cvb, "run") as run:
            self.assertEqual(cvb.select_base_ref("", before, "branch"), before)
        run.assert_called_once_with(
            ["git", "fetch", "--no-tags", "--depth=1", "origin", before],
            check=False,
        )


class PinChoreIntegration(unittest.TestCase):
    """End-to-end manual-mode replay of the stuck v0.1.26 pin wave.

    Nine consumer repos sat red on this exact shape: a pin PR changing only
    `.github/workflows/**`, wearing the approval labels the wave applies, on a
    crate whose version equals both its base and the newest published version.
    """

    def run_gate(
        self,
        *,
        changed,
        labels="release:automated,release:consumer-bump",
        head_branch="automation/kin-actions-pin-next",
        head_repo="kinlab-ai/kin-blobs",
    ):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            argv = [
                "check-version-bump.py",
                "--package",
                "kin-blobs",
                "--manifest",
                "Cargo.toml",
                "--base-ref",
                "origin/main",
                "--version-mode",
                "manual",
                "--event-name",
                "pull_request",
                "--ref-type",
                "branch",
                "--ref-name",
                head_branch,
                "--default-branch",
                "main",
                "--base-repo",
                "kinlab-ai/kin-blobs",
                "--head-repo",
                head_repo,
                "--head-branch",
                head_branch,
                "--labels",
                labels,
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(cvb, "cargo_metadata_version", return_value="0.1.5"),
                mock.patch.object(cvb, "read_manifest_version", return_value="0.1.5"),
                mock.patch.object(cvb, "published_versions", return_value=["0.1.5"]),
                mock.patch.object(cvb, "select_base_ref", return_value="origin/main"),
                mock.patch.object(cvb, "changed_files", return_value=changed),
                mock.patch.object(cvb, "base_manifest_version", return_value="0.1.5"),
                mock.patch.dict(
                    os.environ, {"GITHUB_OUTPUT": str(output)}, clear=False
                ),
            ):
                code = cvb.main()
            values = dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
        return code, values

    def test_workflow_only_pin_pr_passes(self):
        code, values = self.run_gate(
            changed=[".github/workflows/ci.yml", ".github/workflows/release.yml"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(values["release_needed"], "false")
        self.assertEqual(values["release_candidate"], "false")

    def test_same_pr_shape_on_an_ordinary_branch_still_fails(self):
        code, _ = self.run_gate(
            changed=[".github/workflows/ci.yml"], head_branch="chore/hand-edit"
        )
        self.assertEqual(code, 1)

    def test_pin_branch_from_a_fork_still_fails(self):
        code, _ = self.run_gate(
            changed=[".github/workflows/ci.yml"], head_repo="attacker/kin-blobs"
        )
        self.assertEqual(code, 1)

    def test_pin_branch_carrying_crate_source_still_fails(self):
        code, _ = self.run_gate(
            changed=[".github/workflows/ci.yml", "src/lib.rs"]
        )
        self.assertEqual(code, 1)


class TrainModeIntegration(unittest.TestCase):
    def run_gate(
        self,
        *,
        version,
        changed,
        event_name,
        labels,
        generated_paths,
        head_branch="feature",
        release_intent="patch",
    ):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            argv = [
                "check-version-bump.py",
                "--package",
                "x",
                "--manifest",
                "Cargo.toml",
                "--base-ref",
                "base",
                "--version-mode",
                "train",
                "--event-name",
                event_name,
                "--ref-type",
                "branch",
                "--ref-name",
                "main" if event_name == "push" else head_branch,
                "--default-branch",
                "main",
                "--base-repo",
                "firelock-ai/x",
                "--head-repo",
                "firelock-ai/x",
                "--head-branch",
                head_branch,
                "--labels",
                labels,
                "--release-intent",
                release_intent,
            ]
            for path in generated_paths:
                argv.extend(("--generated-path", path))
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(cvb, "cargo_metadata_version", return_value=version),
                mock.patch.object(cvb, "read_manifest_version", return_value=version),
                mock.patch.object(cvb, "published_versions", return_value=["0.1.0"]),
                mock.patch.object(cvb, "select_base_ref", return_value="base"),
                mock.patch.object(cvb, "changed_files", return_value=changed),
                mock.patch.object(cvb, "base_manifest_version", return_value="0.1.0"),
                mock.patch.object(cvb, "verify_generated_release"),
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}, clear=False),
            ):
                code = cvb.main()
            values = dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
        return code, values

    def test_controller_outputs_coalesced_release_intent(self):
        code, values = self.run_gate(
            version="0.1.0",
            changed=["src/lib.rs"],
            event_name="controller",
            labels="release:minor",
            release_intent="minor",
            generated_paths=["Cargo.toml", "Cargo.lock"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(values["release_needed"], "true")
        self.assertEqual(values["release_candidate"], "false")
        self.assertEqual(values["release_intent"], "minor")

    def test_exact_generated_train_pr_owns_candidate(self):
        code, values = self.run_gate(
            version="0.1.1",
            changed=["Cargo.lock"],
            event_name="pull_request",
            labels="release:automated,release:patch",
            generated_paths=["Cargo.lock"],
            head_branch="automation/release-next",
        )
        self.assertEqual(code, 0)
        self.assertEqual(values["release_candidate"], "true")
        self.assertEqual(values["trusted_train_pr"], "true")

    def test_generated_byte_verifier_failure_is_fail_closed(self):
        failed = mock.MagicMock(returncode=1, stderr="byte mismatch", stdout="")
        with mock.patch.object(cvb, "run", return_value=failed):
            with self.assertRaisesRegex(ValueError, "byte mismatch"):
                cvb.verify_generated_release(
                    base_ref="base",
                    package="x",
                    manifest="Cargo.toml",
                    base_version="0.1.0",
                    target_version="0.1.1",
                    generated_paths=["Cargo.toml"],
                )


class RegistryVersions(unittest.TestCase):
    def response(self, body):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        return response

    def test_valid_404_is_the_only_empty_registry(self):
        error = urllib.error.HTTPError(
            "https://kinlab.ai/registry/cargo/1/x", 404, "Not Found", {}, io.BytesIO()
        )
        self.addCleanup(error.close)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(cvb.published_versions("https://kinlab.ai", "x"), [])

    def test_non_404_http_error_fails_closed(self):
        error = urllib.error.HTTPError(
            "https://kinlab.ai/registry/cargo/1/x", 500, "Error", {}, io.BytesIO()
        )
        self.addCleanup(error.close)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(SystemExit, "HTTP 500"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_io_error_fails_closed(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            with self.assertRaisesRegex(SystemExit, "offline"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_invalid_json_row_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen", return_value=self.response(b"not json\n")
        ):
            with self.assertRaisesRegex(SystemExit, "invalid JSON"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_non_object_json_row_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen", return_value=self.response(b'["x"]\n')
        ):
            with self.assertRaisesRegex(SystemExit, "expected an object"):
                cvb.published_versions("https://kinlab.ai", "x")

    def assert_duplicate_rejected(self, body):
        with mock.patch(
            "urllib.request.urlopen", return_value=self.response(body)
        ):
            with self.assertRaisesRegex(
                SystemExit, "duplicate immutable version x@1.0.0"
            ):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_duplicate_version_with_conflicting_checksum_fails_closed(self):
        self.assert_duplicate_rejected(
            registry_row() + registry_row(checksum="1" * 64)
        )

    def test_duplicate_version_with_conflicting_yank_state_fails_closed(self):
        self.assert_duplicate_rejected(
            registry_row() + registry_row(yanked=True)
        )

    def test_identical_duplicate_version_fails_closed(self):
        self.assert_duplicate_rejected(registry_row() + registry_row())

    def test_empty_successful_response_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen", return_value=self.response(b"\n \n")
        ):
            with self.assertRaisesRegex(SystemExit, "empty successful response"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_missing_name_row_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.response(registry_row(omit=("name",))),
        ):
            with self.assertRaisesRegex(SystemExit, "missing string 'name'"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_mismatched_name_row_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.response(registry_row(name="y")),
        ):
            with self.assertRaisesRegex(SystemExit, "does not match"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_missing_version_row_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.response(registry_row(omit=("vers",))),
        ):
            with self.assertRaisesRegex(SystemExit, "missing string 'vers'"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_invalid_semver_row_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.response(registry_row(version="latest")),
        ):
            with self.assertRaisesRegex(SystemExit, "invalid SemVer"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_missing_yanked_row_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.response(registry_row(omit=("yanked",))),
        ):
            with self.assertRaisesRegex(SystemExit, "missing boolean 'yanked'"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_valid_rows_preserve_yanked_versions_in_immutable_history(self):
        body = b"".join(
            (
                registry_row(version="1.0.0-rc.1"),
                registry_row(version="1.0.0"),
                registry_row(version="2.0.0", yanked=True),
            )
        )
        with mock.patch(
            "urllib.request.urlopen", return_value=self.response(body)
        ):
            self.assertEqual(
                cvb.published_versions("https://kinlab.ai", "x"),
                ["1.0.0-rc.1", "1.0.0", "2.0.0"],
            )

    def test_every_request_names_kin_actions_in_its_user_agent(self):
        # Cloudflare refuses urllib's default Python-urllib agent with error
        # 1010 (HTTP 403), so the reader must name itself on every attempt.
        version = (
            Path(__file__).resolve().parent.parent / "VERSION"
        ).read_text(encoding="utf-8").strip()
        expected = (
            f"kin-actions/{version} (+https://github.com/kinlab-ai/kin-actions)"
        )
        self.assertEqual(cvb.registry_index.USER_AGENT, expected)
        error = urllib.error.HTTPError(
            "https://kinlab.ai/registry/cargo/1/x", 503, "Unavailable", {}, io.BytesIO()
        )
        self.addCleanup(error.close)
        with (
            mock.patch(
                "urllib.request.urlopen",
                side_effect=[error, self.response(registry_row())],
            ) as urlopen,
            mock.patch.object(cvb.registry_index.time, "sleep"),
        ):
            self.assertEqual(
                cvb.published_versions("https://kinlab.ai", "x"), ["1.0.0"]
            )
        self.assertEqual(urlopen.call_count, 2)
        for call in urlopen.call_args_list:
            request = call.args[0]
            self.assertEqual(request.full_url, "https://kinlab.ai/registry/cargo/1/x")
            self.assertEqual(request.get_header("User-agent"), expected)


class RegistryReadReasons(unittest.TestCase):
    def reasons(self, **kwargs):
        defaults = dict(
            version_mode="manual",
            version="0.1.5",
            base_version="0.1.5",
            relevant=[],
            release_label=False,
            pin_chore=False,
            event_name="pull_request",
            train_authority=False,
        )
        defaults.update(kwargs)
        return cvb.registry_read_reasons(**defaults)

    def test_change_that_keeps_base_version_and_ships_nothing_needs_no_registry(self):
        self.assertEqual(self.reasons(), [])
        self.assertEqual(self.reasons(event_name="push"), [])
        self.assertEqual(self.reasons(version_mode="train"), [])
        self.assertEqual(self.reasons(version_mode="train", event_name="merge_group"), [])

    def test_every_release_trigger_needs_the_registry(self):
        triggers = {
            "relevant": dict(relevant=["src/lib.rs"]),
            "label": dict(release_label=True),
            "no base": dict(base_version=None),
            "version moved": dict(version="0.1.6"),
            "version downgraded": dict(version="0.1.4"),
            "push that releases": dict(event_name="push", version="0.1.6"),
            "train relevant": dict(version_mode="train", relevant=["src/lib.rs"]),
            "train controller": dict(version_mode="train", event_name="controller"),
            "train PR": dict(version_mode="train", train_authority=True),
        }
        for name, kwargs in triggers.items():
            with self.subTest(name):
                self.assertTrue(self.reasons(**kwargs))

    def test_pin_chore_label_needs_no_registry(self):
        self.assertEqual(self.reasons(release_label=True, pin_chore=True), [])

    def test_train_mode_labels_force_no_registry_read(self):
        self.assertEqual(self.reasons(version_mode="train", release_label=True), [])


class RegistryOutage(unittest.TestCase):
    """Replay the gate end to end while the registry answers HTTP 503.

    A change that keeps the base version and ships nothing must pass without
    reading the registry. Every change whose verdict depends on registry
    history must still stop on the outage.
    """

    OUTAGE = (
        "could not read registry index "
        "https://kinlab.ai/registry/cargo/ki/n-/kin-blobs: HTTP 503 after 4 attempts"
    )

    def run_gate(
        self,
        *,
        changed,
        version="0.1.5",
        labels="",
        event_name="pull_request",
        version_mode="manual",
    ):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            argv = [
                "check-version-bump.py",
                "--package",
                "kin-blobs",
                "--manifest",
                "Cargo.toml",
                "--base-ref",
                "origin/main",
                "--version-mode",
                version_mode,
                "--event-name",
                event_name,
                "--ref-type",
                "branch",
                "--ref-name",
                "main" if event_name == "push" else "fix/workflow-permissions",
                "--default-branch",
                "main",
                "--base-repo",
                "kinlab-ai/kin-blobs",
                "--head-repo",
                "kinlab-ai/kin-blobs",
                "--head-branch",
                "fix/workflow-permissions",
                "--labels",
                labels,
                "--generated-path",
                "Cargo.toml",
                "--generated-path",
                "Cargo.lock",
            ]
            stdout = io.StringIO()
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(cvb, "cargo_metadata_version", return_value=version),
                mock.patch.object(cvb, "read_manifest_version", return_value=version),
                mock.patch.object(
                    cvb, "published_versions", side_effect=SystemExit(self.OUTAGE)
                ) as published,
                mock.patch.object(cvb, "select_base_ref", return_value="origin/main"),
                mock.patch.object(cvb, "changed_files", return_value=changed),
                mock.patch.object(cvb, "base_manifest_version", return_value="0.1.5"),
                mock.patch.object(cvb, "verify_generated_release"),
                mock.patch.dict(
                    os.environ, {"GITHUB_OUTPUT": str(output)}, clear=False
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = cvb.main()
            values = dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
        return code, values, stdout.getvalue(), published

    def assert_passes_without_registry(self, **kwargs):
        code, values, stdout, published = self.run_gate(**kwargs)
        self.assertEqual(code, 0, stdout)
        published.assert_not_called()
        self.assertEqual(values["release_needed"], "false")
        self.assertEqual(values["release_candidate"], "false")
        self.assertIn("registry read     : no", stdout)
        self.assertIn("newest published  : <not read>", stdout)
        self.assertIn("The registry was not read", stdout)

    def assert_fails_closed(self, **kwargs):
        with self.assertRaisesRegex(SystemExit, "HTTP 503"):
            self.run_gate(**kwargs)

    def test_workflow_only_pr_passes_while_registry_is_down(self):
        self.assert_passes_without_registry(
            changed=[
                ".github/workflows/ci.yml",
                ".github/workflows/registry-publish.yml",
            ]
        )

    def test_docs_and_tests_only_pr_passes_while_registry_is_down(self):
        self.assert_passes_without_registry(changed=["README.md", "tests/it.rs"])

    def test_workflow_only_main_push_passes_while_registry_is_down(self):
        self.assert_passes_without_registry(
            changed=[".github/workflows/ci.yml"], event_name="push"
        )

    def test_train_mode_workflow_only_pr_passes_while_registry_is_down(self):
        self.assert_passes_without_registry(
            changed=[".github/workflows/ci.yml"], version_mode="train"
        )

    def test_source_change_fails_closed_while_registry_is_down(self):
        self.assert_fails_closed(changed=["src/lib.rs"], version="0.1.6")

    def test_version_move_fails_closed_while_registry_is_down(self):
        self.assert_fails_closed(changed=[".github/workflows/ci.yml"], version="0.1.6")

    def test_release_label_fails_closed_while_registry_is_down(self):
        self.assert_fails_closed(changed=[".github/workflows/ci.yml"], labels="release")

    def test_releasing_main_push_fails_closed_while_registry_is_down(self):
        self.assert_fails_closed(
            changed=[".github/workflows/ci.yml"], version="0.1.6", event_name="push"
        )

    def test_train_controller_fails_closed_while_registry_is_down(self):
        self.assert_fails_closed(
            changed=[".github/workflows/ci.yml"],
            version_mode="train",
            event_name="controller",
        )

    def test_train_source_pr_fails_closed_while_registry_is_down(self):
        self.assert_fails_closed(changed=["src/lib.rs"], version_mode="train")


if __name__ == "__main__":
    unittest.main()
