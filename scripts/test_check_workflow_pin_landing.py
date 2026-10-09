"""Tests for exact merged workflow-pin PR provenance."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("check-workflow-pin-landing.py")
SPEC = importlib.util.spec_from_file_location("check_workflow_pin_landing", SCRIPT)
landing = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = landing
SPEC.loader.exec_module(landing)


class WorkflowPinLandingTests(unittest.TestCase):
    merge_sha = "a" * 40

    def pull(self, **overrides) -> dict:
        value = {
            "number": 12,
            "state": "closed",
            "merged_at": "2026-08-31T00:00:00Z",
            "title": "chore(ci): pin kin-actions v0.1.34",
            "merge_commit_sha": self.merge_sha,
            "head": {
                "sha": "c" * 40,
                "ref": "automation/kin-actions-pin-next",
                "repo": {"full_name": "kinlab-ai/kin-blobs"},
            },
            "base": {"ref": "main"},
        }
        value.update(overrides)
        return value

    def compare(self, **overrides) -> dict:
        value = {"status": "ahead", "base_commit": {"sha": self.merge_sha}}
        value.update(overrides)
        return value

    def check_runs(self, conclusion: str = "success") -> list[dict]:
        return [
            {
                "name": name,
                "status": "completed",
                "conclusion": conclusion,
                "app": {"id": 15368},
            }
            for name in sorted(landing.protection_guard.CARGO_CONTEXTS)
        ]

    def required(self, bucket: str = "pass") -> list[dict]:
        return [
            {"name": name, "bucket": bucket}
            for name in sorted(landing.protection_guard.CARGO_CONTEXTS)
        ]

    def settings(self) -> dict:
        return {
            "full_name": "kinlab-ai/kin-blobs",
            "default_branch": "main",
            **landing.protection_guard.EXPECTED_SETTINGS,
        }

    def status_checks(self) -> dict:
        contexts = sorted(landing.protection_guard.CARGO_CONTEXTS)
        return {
            "strict": True,
            "contexts": contexts,
            "checks": [
                {"context": context, "app_id": 15368} for context in contexts
            ],
        }

    def evaluate(self, **overrides):
        values = {
            "pulls": [self.pull()],
            "compare": self.compare(),
            "repository": "kinlab-ai/kin-blobs",
            "base": "main",
            "head_branch": "automation/kin-actions-pin-next",
            "target_version": "0.1.34",
            "landed_files": {
                ".github/workflows/registry-publish.yml": (
                    b"    uses: kinlab-ai/kin-actions/.github/workflows/"
                    b"cargo-registry-release.yml@v0.1.33\n",
                    b"    uses: kinlab-ai/kin-actions/.github/workflows/"
                    b"cargo-registry-release.yml@v0.1.34\n",
                )
            },
            "check_runs": self.check_runs(),
            "required_checks": self.required(),
            "repository_settings": self.settings(),
            "status_checks": self.status_checks(),
            "kind": "cargo_release",
            "required_app_id": 15368,
            "allowed_paths": [
                ".github/workflows/registry-publish.yml",
                ".github/workflows/scheduled-failure-alarm.yml",
            ],
        }
        values.update(overrides)
        return landing.evaluate_landing(**values)

    def test_exact_merged_pr_on_main_is_proven(self) -> None:
        self.assertEqual(self.evaluate()["status"], "proven")

    def test_absent_merge_waits(self) -> None:
        self.assertEqual(
            self.evaluate(pulls=[], compare=None)["status"], "waiting"
        )

    def test_foreign_head_duplicate_and_nonancestor_fail_closed(self) -> None:
        foreign = self.pull()
        foreign["head"]["repo"]["full_name"] = "attacker/fork"
        self.assertEqual(
            self.evaluate(pulls=[foreign], compare=None)["status"], "waiting"
        )
        with self.assertRaisesRegex(landing.LandingError, "multiple merged"):
            self.evaluate(pulls=[self.pull(), self.pull(number=13)])
        with self.assertRaisesRegex(landing.LandingError, "not an ancestor"):
            self.evaluate(compare=self.compare(status="diverged"))

    def test_missing_pr_number_and_mismatched_compare_base_fail_closed(self) -> None:
        with self.assertRaisesRegex(landing.LandingError, "positive number"):
            self.evaluate(pulls=[self.pull(number=None)])
        with self.assertRaisesRegex(landing.LandingError, "exact pin merge"):
            self.evaluate(compare=self.compare(base_commit={"sha": "b" * 40}))

    def test_extra_path_and_skipped_cargo_proof_fail_closed(self) -> None:
        with self.assertRaisesRegex(landing.LandingError, "non-manifest"):
            self.evaluate(
                landed_files={"README.md": (b"old\n", b"new\n")}
            )
        checks = self.check_runs()
        checks[0]["conclusion"] = "skipped"
        required = self.required()
        required[0]["bucket"] = "skipping"
        with self.assertRaisesRegex(landing.LandingError, "Cargo proof"):
            self.evaluate(check_runs=checks, required_checks=required)

    def test_allowed_workflow_cannot_carry_non_pin_bytes(self) -> None:
        with self.assertRaisesRegex(landing.LandingError, "beyond canonical pins"):
            self.evaluate(
                landed_files={
                    ".github/workflows/registry-publish.yml": (
                        b"    timeout-minutes: 10\n",
                        b"    timeout-minutes: 1\n",
                    )
                }
            )

    def test_full_landed_blob_rejects_changes_hidden_by_a_truncated_rest_patch(self) -> None:
        before = (
            b"    uses: kinlab-ai/kin-actions/.github/workflows/"
            b"cargo-registry-release.yml@v0.1.33\n"
            b"    timeout-minutes: 10\n"
        )
        after = (
            b"    uses: kinlab-ai/kin-actions/.github/workflows/"
            b"cargo-registry-release.yml@v0.1.34\n"
            b"    timeout-minutes: 1\n"
        )
        with self.assertRaisesRegex(landing.LandingError, "beyond canonical pins"):
            self.evaluate(
                landed_files={
                    ".github/workflows/registry-publish.yml": (before, after)
                }
            )

    def test_cross_file_pin_swap_cannot_balance_global_counters(self) -> None:
        prefix = b"    uses: kinlab-ai/kin-actions/.github/workflows/"
        with self.assertRaisesRegex(landing.LandingError, "beyond canonical pins"):
            self.evaluate(
                landed_files={
                    ".github/workflows/registry-publish.yml": (
                        prefix + b"cargo-registry-release.yml@v0.1.33\n",
                        prefix + b"scheduled-failure-alarm.yml@v0.1.34\n",
                    ),
                    ".github/workflows/scheduled-failure-alarm.yml": (
                        prefix + b"scheduled-failure-alarm.yml@v0.1.33\n",
                        prefix + b"cargo-registry-release.yml@v0.1.34\n",
                    ),
                }
            )

    def test_newer_removed_version_cannot_prove_a_rollout(self) -> None:
        prefix = b"    uses: kinlab-ai/kin-actions/.github/workflows/"
        with self.assertRaisesRegex(landing.LandingError, "older stable version"):
            self.evaluate(
                landed_files={
                    ".github/workflows/registry-publish.yml": (
                        prefix + b"cargo-registry-release.yml@v0.1.35\n",
                        prefix + b"cargo-registry-release.yml@v0.1.34\n",
                    )
                }
            )

    def test_exact_git_object_reader_returns_complete_parent_commit_blobs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.com"],
                check=True,
            )
            path = root / ".github/workflows/registry-publish.yml"
            path.parent.mkdir(parents=True)
            before = (
                "    uses: kinlab-ai/kin-actions/.github/workflows/"
                "cargo-registry-release.yml@v0.1.33\n"
            )
            after = before.replace("0.1.33", "0.1.34")
            path.write_text(before, encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-q", "-s", "-m", "base"],
                check=True,
            )
            path.write_text(after, encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-q", "-s", "-m", "pin"],
                check=True,
            )
            merge_sha = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            changes = landing._read_landed_object_changes(root, merge_sha)
            self.assertEqual(
                changes,
                {
                    ".github/workflows/registry-publish.yml": (
                        before.encode(),
                        after.encode(),
                    )
                },
            )


if __name__ == "__main__":
    unittest.main()
