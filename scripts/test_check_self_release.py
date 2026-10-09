"""Adversarial tests for kin-actions self-release authority."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


# A branch whose name merely contains the queue prefix must never be mistaken
# for a ref GitHub's merge queue minted.
QUEUE_REF_SPOOF = "feature/gh-readonly-queue/main"

SCRIPT = Path(__file__).with_name("check-self-release.py")
SPEC = importlib.util.spec_from_file_location("check_self_release", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


class SelfReleaseAuthorityTests(unittest.TestCase):
    def evaluate(self, **changes):
        values = {
            "current_version": "0.1.22",
            "base_version": "0.1.22",
            "changed_paths": ["scripts/tool.py"],
            "event_name": "pull_request",
            "ref_type": "branch",
            "ref_name": "feature",
            "default_branch": "main",
            "base_repo": "kinlab-ai/kin-actions",
            "head_repo": "kinlab-ai/kin-actions",
            "head_branch": "feature",
            "labels": [],
            "remote_tag_sha": None,
            "head_sha": "a" * 40,
            "pin_failures": [],
        }
        values.update(changes)
        return gate.evaluate(**values)

    def generated(self, **changes):
        values = {
            "current_version": "0.1.23",
            "changed_paths": list(gate.ALLOWED_PATHS),
            "head_branch": gate.TRAIN_BRANCH,
            "labels": ["release:automated", "release:patch"],
        }
        values.update(changes)
        return self.evaluate(**values)

    def test_ordinary_pr_cannot_move_version(self) -> None:
        result = self.evaluate(
            current_version="0.1.23",
            changed_paths=["VERSION"],
        )
        self.assertTrue(any("only the exact" in item for item in result["failures"]))

    def test_exact_first_party_generated_pr_is_candidate(self) -> None:
        result = self.generated()
        self.assertEqual(result["failures"], [])
        self.assertTrue(result["release_candidate"])
        self.assertEqual(result["intent"], "patch")

    def test_fork_label_branch_and_extra_path_each_fail(self) -> None:
        cases = (
            {"head_repo": "attacker/kin-actions"},
            {"labels": ["release:patch"]},
            {"head_branch": "automation/lookalike"},
            {"changed_paths": [*gate.ALLOWED_PATHS, "scripts/tool.py"]},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertTrue(self.generated(**changes)["failures"])

    def test_patch_minor_major_must_be_exact_successors(self) -> None:
        for version, intent in (
            ("0.1.23", "patch"),
            ("0.2.0", "minor"),
            ("1.0.0", "major"),
        ):
            with self.subTest(version=version):
                result = self.generated(current_version=version)
                self.assertEqual(result["failures"], [])
                self.assertEqual(result["intent"], intent)
        self.assertTrue(
            self.generated(current_version="0.1.24")["failures"]
        )

    def test_existing_remote_tag_blocks_pr(self) -> None:
        result = self.generated(remote_tag_sha="b" * 40)
        self.assertTrue(any("already exists" in item for item in result["failures"]))

    def test_main_push_allows_exact_generated_successor(self) -> None:
        result = self.generated(
            event_name="push",
            ref_name="main",
            head_branch="",
            labels=[],
        )
        self.assertEqual(result["failures"], [])
        self.assertTrue(result["release_candidate"])

    def test_main_rerun_allows_tag_only_at_exact_head(self) -> None:
        exact = self.generated(
            event_name="push",
            ref_name="main",
            remote_tag_sha="a" * 40,
            head_branch="",
            labels=[],
        )
        self.assertEqual(exact["failures"], [])
        wrong = self.generated(
            event_name="push",
            ref_name="main",
            remote_tag_sha="b" * 40,
            head_branch="",
            labels=[],
        )
        self.assertTrue(any("not exact head" in item for item in wrong["failures"]))

    def test_non_default_push_and_pin_mismatch_fail(self) -> None:
        branch = self.generated(
            event_name="push",
            ref_name="other",
            head_branch="",
            labels=[],
        )
        self.assertTrue(any("default branch" in item for item in branch["failures"]))
        pins = self.generated(pin_failures=["README pin mismatch"])
        self.assertIn("README pin mismatch", pins["failures"])

    def test_no_version_movement_is_not_a_release_candidate(self) -> None:
        result = self.evaluate()
        self.assertEqual(result["failures"], [])
        self.assertFalse(result["release_candidate"])

    def test_document_pins_must_all_equal_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, version in (
                ("README.md", "0.1.23"),
                ("CONTRIBUTING.md", "0.1.22"),
            ):
                (root / name).write_text(
                    "uses: kinlab-ai/kin-actions/.github/workflows/"
                    f"x.yml@v{version}\n",
                    encoding="utf-8",
                )
            failures = gate.validate_document_pins(root, "0.1.23")
            self.assertTrue(any("CONTRIBUTING.md" in item for item in failures))

    def test_base_selection_fails_closed_without_exact_branch_before(self) -> None:
        with self.assertRaises(gate.SelfReleaseGateError):
            gate.select_base_ref("", "", "branch")
        with self.assertRaises(gate.SelfReleaseGateError):
            gate.select_base_ref("", "0" * 40, "branch")
        with self.assertRaises(gate.SelfReleaseGateError):
            gate.select_base_ref("", "a" * 40, "tag")


class MergeQueueValidationTests(unittest.TestCase):
    """A queue run reports the required context without release authority."""

    QUEUE_REF = f"gh-readonly-queue/main/pr-20-{'f' * 40}"

    def classify(self, **changes):
        values = {
            "event_name": "merge_group",
            "ref_type": "branch",
            "ref_name": self.QUEUE_REF,
            "default_branch": "main",
        }
        values.update(changes)
        return gate.is_queue_validation(**values)

    def run_gate(self, argv: list[str], version: str = "0.1.22\n"):
        """Run the gate in a throwaway tree and return (code, stdout+stderr)."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text(version, encoding="utf-8")
            output = root / "github-output"
            previous = Path.cwd()
            stream = io.StringIO()
            os.chdir(root)
            try:
                with unittest.mock.patch.dict(
                    os.environ, {"GITHUB_OUTPUT": str(output)}
                ):
                    with contextlib.redirect_stdout(stream):
                        with contextlib.redirect_stderr(stream):
                            code = gate.main(argv)
            finally:
                os.chdir(previous)
            written = (
                output.read_text(encoding="utf-8") if output.exists() else ""
            )
        return code, stream.getvalue(), written

    def test_exact_queue_ref_is_classified_from_the_default_branch(self) -> None:
        self.assertTrue(self.classify())
        self.assertTrue(
            self.classify(
                ref_name=f"gh-readonly-queue/release/pr-7-{'0' * 40}",
                default_branch="release",
            )
        )
        # The pattern is built from the default branch, so a queue ref naming a
        # different branch is not this repository's queue.
        with self.assertRaises(gate.SelfReleaseGateError):
            self.classify(default_branch="release")

    def test_malformed_queue_refs_fail_loud(self) -> None:
        cases = (
            {"ref_name": ""},
            {"ref_name": "main"},
            {"ref_name": f"gh-readonly-queue/main/pr-20-{'f' * 39}"},
            {"ref_name": f"gh-readonly-queue/main/pr-20-{'g' * 40}"},
            {"ref_name": f"gh-readonly-queue/main/pr--{'f' * 40}"},
            {"ref_name": f"gh-readonly-queue/main/pr-0-{'f' * 40}"},
            {"ref_name": f"refs/heads/gh-readonly-queue/main/pr-20-{'f' * 40}"},
            {"ref_name": f"{QUEUE_REF_SPOOF}/pr-20-{'f' * 40}"},
            {"ref_type": "tag"},
            {"default_branch": ""},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(gate.SelfReleaseGateError):
                    self.classify(**changes)

    def test_other_events_are_never_queue_validation(self) -> None:
        for event in ("push", "pull_request", "workflow_dispatch", ""):
            with self.subTest(event=event):
                self.assertFalse(self.classify(event_name=event))

    def test_queue_run_passes_without_a_before_object_id(self) -> None:
        code, printed, written = self.run_gate(
            [
                "--event-name", "merge_group",
                "--ref-type", "branch",
                "--ref-name", self.QUEUE_REF,
                "--default-branch", "main",
                "--base-repo", "kinlab-ai/kin-actions",
            ]
        )
        self.assertEqual(code, 0, printed)
        self.assertIn("merge-queue validation", printed)
        self.assertIn("release_candidate=false", written)
        self.assertIn("intent=\n", written)

    def test_queue_run_with_a_malformed_ref_fails(self) -> None:
        code, printed, _ = self.run_gate(
            [
                "--event-name", "merge_group",
                "--ref-type", "branch",
                "--ref-name", "gh-readonly-queue/main/pr-20-deadbeef",
                "--default-branch", "main",
                "--base-repo", "kinlab-ai/kin-actions",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("merge-queue ref", printed)

    def test_queue_run_still_requires_an_exact_version_file(self) -> None:
        code, printed, _ = self.run_gate(
            [
                "--event-name", "merge_group",
                "--ref-type", "branch",
                "--ref-name", self.QUEUE_REF,
                "--default-branch", "main",
                "--base-repo", "kinlab-ai/kin-actions",
            ],
            version="not-a-version\n",
        )
        self.assertEqual(code, 1)

    def test_push_without_a_before_object_id_still_fails(self) -> None:
        code, printed, _ = self.run_gate(
            [
                "--event-name", "push",
                "--ref-type", "branch",
                "--ref-name", "main",
                "--push-before", "",
                "--default-branch", "main",
                "--base-repo", "kinlab-ai/kin-actions",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("exact before object ID", printed)


if __name__ == "__main__":
    unittest.main()
