"""Unit tests for the exact kin-actions consumer pin updater."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("update-kin-actions-pins.py")
SPEC = importlib.util.spec_from_file_location("update_kin_actions_pins", SCRIPT)
pins = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = pins
SPEC.loader.exec_module(pins)


class PinUpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "consumers.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def configure(self, paths: list[str]) -> None:
        self.manifest.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "repositories": {"firelock-ai/example": paths},
                }
            ),
            encoding="utf-8",
        )

    def workflow(self, relative: str, version: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "jobs:\n"
            "  release:\n"
            "    uses: kinlab-ai/kin-actions/.github/workflows/"
            f"cargo-registry-release.yml@v{version}\n",
            encoding="utf-8",
        )
        return path

    def update(self, version: str = "0.1.23"):
        return pins.update_pins(
            root=self.root,
            manifest=self.manifest,
            repository="firelock-ai/example",
            target_version=version,
        )

    def test_updates_exact_manifest_allowlisted_workflows(self) -> None:
        allowed = self.workflow(".github/workflows/release.yml", "0.1.21")
        self.configure([".github/workflows/release.yml"])
        result = self.update()
        self.assertIn("@v0.1.23", allowed.read_text())
        self.assertEqual(
            result["changed_paths"], [".github/workflows/release.yml"]
        )

    def test_unmanifested_live_pin_fails_without_partial_writes(self) -> None:
        allowed = self.workflow(".github/workflows/release.yml", "0.1.21")
        omitted = self.workflow(".github/workflows/not-listed.yml", "0.1.21")
        self.configure([".github/workflows/release.yml"])
        before_allowed = allowed.read_bytes()
        before_omitted = omitted.read_bytes()
        with self.assertRaisesRegex(pins.PinUpdateError, "unmanifested live pins"):
            self.update()
        self.assertEqual(allowed.read_bytes(), before_allowed)
        self.assertEqual(omitted.read_bytes(), before_omitted)

    def test_stale_manifest_path_fails_without_writes(self) -> None:
        live = self.workflow(".github/workflows/release.yml", "0.1.21")
        stale = self.root / ".github/workflows/removed.yml"
        stale.write_text("jobs: {}\n", encoding="utf-8")
        self.configure(
            [
                ".github/workflows/release.yml",
                ".github/workflows/removed.yml",
            ]
        )
        before = live.read_bytes()
        with self.assertRaisesRegex(
            pins.PinUpdateError, "manifest paths without live pins"
        ):
            self.update()
        self.assertEqual(live.read_bytes(), before)

    def test_exact_target_is_idempotent(self) -> None:
        self.workflow(".github/workflows/release.yml", "0.1.23")
        self.configure([".github/workflows/release.yml"])
        result = self.update()
        self.assertFalse(result["changed"])
        self.assertEqual(result["changed_paths"], [])

    def test_newer_pin_refuses_downgrade_without_partial_writes(self) -> None:
        older = self.workflow(".github/workflows/a.yml", "0.1.21")
        newer = self.workflow(".github/workflows/b.yml", "0.1.24")
        self.configure(
            [".github/workflows/a.yml", ".github/workflows/b.yml"]
        )
        before_older = older.read_bytes()
        before_newer = newer.read_bytes()
        with self.assertRaisesRegex(pins.PinUpdateError, "downgrade"):
            self.update()
        self.assertEqual(older.read_bytes(), before_older)
        self.assertEqual(newer.read_bytes(), before_newer)

    def test_missing_exact_pin_fails_closed(self) -> None:
        path = self.root / ".github/workflows/release.yml"
        path.parent.mkdir(parents=True)
        path.write_text("jobs: {}\n", encoding="utf-8")
        self.configure([".github/workflows/release.yml"])
        with self.assertRaisesRegex(
            pins.PinUpdateError, "manifest paths without live pins"
        ):
            self.update()

    def test_symlink_workflow_fails_closed(self) -> None:
        target = self.workflow("target.yml", "0.1.21")
        workflow = self.root / ".github/workflows/release.yml"
        workflow.parent.mkdir(parents=True)
        os.symlink(target, workflow)
        self.configure([".github/workflows/release.yml"])
        with self.assertRaisesRegex(pins.PinUpdateError, "regular file"):
            self.update()

    def test_manifest_rejects_escape_nonworkflow_and_duplicate(self) -> None:
        bad_sets = (
            ["../release.yml"],
            ["README.md"],
            [".github/workflows/a.yml", ".github/workflows/a.yml"],
        )
        for paths in bad_sets:
            with self.subTest(paths=paths):
                self.configure(paths)
                with self.assertRaises(pins.PinUpdateError):
                    pins.load_consumer_paths(
                        self.manifest, "firelock-ai/example"
                    )

    def test_unlisted_repository_fails_closed(self) -> None:
        self.configure([".github/workflows/release.yml"])
        with self.assertRaisesRegex(pins.PinUpdateError, "not allowlisted"):
            pins.load_consumer_paths(self.manifest, "firelock-ai/other")

    def test_stable_numeric_semver_only(self) -> None:
        for version in ("0.1.23-rc.1", "v0.1.23", "00.1.23", "0.1"):
            with self.subTest(version=version), self.assertRaises(
                pins.PinUpdateError
            ):
                pins.parse_version(version)

    def test_mode_is_preserved(self) -> None:
        path = self.workflow(".github/workflows/release.yml", "0.1.21")
        path.chmod(0o640)
        self.configure([".github/workflows/release.yml"])
        self.update()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def quoted_workflow(self, relative: str, ref: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "jobs:\n"
            "  release:\n"
            '    uses: "kinlab-ai/kin-actions/.github/workflows/'
            f'cargo-registry-release.yml@{ref}"\n',
            encoding="utf-8",
        )
        return path

    def test_quoted_pin_cannot_escape_the_inventory(self) -> None:
        self.workflow(".github/workflows/release.yml", "0.1.21")
        self.quoted_workflow(".github/workflows/quoted.yml", "v0.1.21")
        self.configure([".github/workflows/release.yml"])
        with self.assertRaisesRegex(pins.PinUpdateError, "unmanifested live pins"):
            self.update()

    def test_quoted_pin_refuses_a_partial_rewrite(self) -> None:
        quoted = self.quoted_workflow(".github/workflows/release.yml", "v0.1.21")
        self.configure([".github/workflows/release.yml"])
        before = quoted.read_bytes()
        with self.assertRaisesRegex(pins.PinUpdateError, "refusing a partial rewrite"):
            self.update()
        self.assertEqual(quoted.read_bytes(), before)

    def test_comment_cannot_substitute_for_a_folded_semantic_pin(self) -> None:
        path = self.root / ".github/workflows/release.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# uses: kinlab-ai/kin-actions/.github/workflows/"
            "cargo-registry-release.yml@v0.1.21\n"
            "jobs:\n"
            "  release:\n"
            "    uses: >-\n"
            "      kinlab-ai/kin-actions/.github/workflows/"
            "cargo-registry-release.yml@v0.1.21\n",
            encoding="utf-8",
        )
        self.configure([".github/workflows/release.yml"])
        before = path.read_bytes()
        with self.assertRaisesRegex(
            pins.PinUpdateError, "comments, folded scalars"
        ):
            self.update()
        self.assertEqual(path.read_bytes(), before)

    def test_mutable_kin_actions_ref_fails_closed(self) -> None:
        for ref in ("main", "v0.1", "0.1.21"):
            with self.subTest(ref=ref):
                path = self.quoted_workflow(".github/workflows/release.yml", ref)
                self.configure([".github/workflows/release.yml"])
                with self.assertRaisesRegex(
                    pins.PinUpdateError, "not pinned to one stable release tag"
                ):
                    self.update()
                path.unlink()

    def test_two_release_bootstrap_fails_closed_then_updates_full_inventory(self) -> None:
        release_a = self.workflow(
            ".github/workflows/registry-publish.yml", "0.2.0"
        )
        self.configure([".github/workflows/registry-publish.yml"])
        self.assertFalse(self.update("0.2.0")["changed"])

        future = self.workflow(
            ".github/workflows/release-train.yml", "0.2.0"
        )
        before_a = release_a.read_bytes()
        before_future = future.read_bytes()
        with self.assertRaisesRegex(
            pins.PinUpdateError, "unmanifested live pins"
        ):
            self.update("0.2.1")
        self.assertEqual(release_a.read_bytes(), before_a)
        self.assertEqual(future.read_bytes(), before_future)

        self.configure(
            [
                ".github/workflows/registry-publish.yml",
                ".github/workflows/release-train.yml",
            ]
        )
        result = self.update("0.2.1")
        self.assertEqual(
            result["changed_paths"],
            [
                ".github/workflows/registry-publish.yml",
                ".github/workflows/release-train.yml",
            ],
        )
        self.assertIn("@v0.2.1", release_a.read_text(encoding="utf-8"))
        self.assertIn("@v0.2.1", future.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
