"""Adversarial unit tests for automatic train version authority."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("release_train_policy.py")
SPEC = importlib.util.spec_from_file_location("release_train_policy", SCRIPT)
policy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)


class TrainPolicyTests(unittest.TestCase):
    def gate(self, **changes):
        values = {
            "package": "kin-demo",
            "version": "0.1.0",
            "base_version": "0.1.0",
            "published": ["0.1.0"],
            "relevant_paths": [],
            "changed_paths": [],
            "generated_paths": ["Cargo.toml", "Cargo.lock"],
            "labels": [],
            "release_intent": "patch",
            "event_name": "pull_request",
            "ref_type": "branch",
            "ref_name": "feature",
            "default_branch": "main",
            "base_repo": "kinlab-ai/kin-demo",
            "head_repo": "kinlab-ai/kin-demo",
            "head_branch": "feature",
            "train_branch": "automation/release-next",
        }
        values.update(changes)
        return policy.evaluate_train_gate(**values)

    def test_ordinary_source_pr_passes_without_version_edit(self) -> None:
        result = self.gate(relevant_paths=["src/lib.rs"])
        self.assertEqual(result["failures"], [])
        self.assertTrue(result["release_needed"])
        self.assertFalse(result["release_candidate"])

    def test_ordinary_manual_version_edit_fails(self) -> None:
        result = self.gate(version="0.1.1", changed_paths=["Cargo.toml"])
        self.assertTrue(any("manual version edits" in item for item in result["failures"]))

    def test_first_party_branch_and_exact_label_are_both_required(self) -> None:
        base = {
            "version": "0.1.1",
            "changed_paths": ["Cargo.toml", "Cargo.lock"],
            "labels": ["release:automated", "release:patch"],
            "head_branch": "automation/release-next",
        }
        self.assertTrue(self.gate(**base)["trusted_train_pr"])
        self.assertFalse(
            self.gate(**(base | {"head_repo": "attacker/fork"}))["trusted_train_pr"]
        )
        self.assertFalse(
            self.gate(**(base | {"labels": ["release:patch"]}))["trusted_train_pr"]
        )

    def test_generated_patch_minor_and_major_are_inferred_from_bytes(self) -> None:
        cases = (
            ("release:patch", "0.1.1"),
            ("release:minor", "0.2.0"),
            ("release:major", "1.0.0"),
        )
        for label, target in cases:
            with self.subTest(label=label):
                result = self.gate(
                    version=target,
                    changed_paths=["Cargo.toml", "Cargo.lock"],
                    labels=["release:automated", label],
                    # A mutable label/argument mismatch cannot downgrade the
                    # immutable committed target.
                    release_intent="patch",
                    head_branch="automation/release-next",
                )
                self.assertEqual(result["failures"], [])
                self.assertTrue(result["release_candidate"])
                self.assertEqual(
                    result["release_intent"],
                    target == "1.0.0"
                    and "major"
                    or target == "0.2.0"
                    and "minor"
                    or "patch",
                )

    def test_generated_wrong_successor_and_extra_path_fail(self) -> None:
        result = self.gate(
            version="0.1.7",
            changed_paths=["Cargo.toml", "src/lib.rs"],
            labels=["release:automated", "release:patch"],
            head_branch="automation/release-next",
        )
        self.assertTrue(any("must move" in item for item in result["failures"]))
        self.assertTrue(any("non-generated" in item for item in result["failures"]))

    def test_main_version_push_requires_generated_only_single_successor(self) -> None:
        result = self.gate(
            event_name="push",
            ref_name="main",
            version="0.2.0",
            changed_paths=["Cargo.toml", "Cargo.lock"],
        )
        self.assertEqual(result["failures"], [])
        self.assertTrue(result["release_candidate"])
        bad = self.gate(
            event_name="push",
            ref_name="main",
            version="0.3.0",
            changed_paths=["Cargo.toml"],
        )
        self.assertTrue(any("automatic successor" in item for item in bad["failures"]))

    def test_tag_push_never_reenters_publication(self) -> None:
        result = self.gate(
            event_name="push",
            ref_type="tag",
            version="0.1.1",
            published=["0.1.0", "0.1.1"],
            changed_paths=["Cargo.toml"],
        )
        self.assertEqual(result["failures"], [])
        self.assertFalse(result["release_candidate"])
        self.assertFalse(result["release_needed"])

    def test_non_default_branch_push_has_no_version_authority(self) -> None:
        result = self.gate(
            event_name="push",
            ref_name="feature",
            version="0.1.1",
            changed_paths=["Cargo.toml", "Cargo.lock"],
        )
        self.assertTrue(
            any("exact default branch" in item for item in result["failures"])
        )
        self.assertFalse(result["release_candidate"])

    def test_published_generated_target_fails(self) -> None:
        result = self.gate(
            version="0.1.1",
            published=["0.1.0", "0.1.1"],
            changed_paths=["Cargo.toml"],
            labels=["release:automated"],
            head_branch="automation/release-next",
        )
        self.assertTrue(
            any("not newer than immutable" in item for item in result["failures"])
        )

    def test_unpublished_base_cannot_anchor_an_automatic_train(self) -> None:
        result = self.gate(published=[])
        self.assertTrue(
            any("absent from immutable registry history" in item
                for item in result["failures"])
        )

    def test_yanked_rows_still_block_immutable_version_reuse(self) -> None:
        # The registry reader deliberately retains yanked rows. The train
        # policy receives the full immutable history, not only installable
        # versions.
        result = self.gate(
            version="0.1.1",
            published=["0.1.0", "0.1.1"],
            changed_paths=["Cargo.toml"],
            labels=["release:automated"],
            head_branch="automation/release-next",
        )
        self.assertTrue(
            any("not newer than immutable" in item for item in result["failures"])
        )

    def test_published_prerelease_constrains_stable_target(self) -> None:
        result = self.gate(
            version="0.1.1",
            published=["0.1.0", "0.2.0-alpha-1"],
            changed_paths=["Cargo.toml"],
            labels=["release:automated"],
            head_branch="automation/release-next",
        )
        self.assertTrue(
            any("lower than newest published" in item for item in result["failures"])
        )

    def test_build_metadata_does_not_create_fresh_precedence(self) -> None:
        result = self.gate(
            version="0.1.1",
            published=["0.1.1+build.7"],
            changed_paths=["Cargo.toml"],
            labels=["release:automated"],
            head_branch="automation/release-next",
        )
        self.assertTrue(
            any("not newer than immutable" in item for item in result["failures"])
        )

    def test_controller_coalesces_highest_intent(self) -> None:
        result = self.gate(
            event_name="controller",
            relevant_paths=["src/lib.rs"],
            labels=["release:patch"],
            release_intent="major",
        )
        self.assertEqual(result["release_intent"], "major")
        self.assertTrue(result["release_needed"])

    def test_mutable_release_labels_have_zero_controller_authority(self) -> None:
        major = self.gate(
            event_name="controller",
            relevant_paths=["src/lib.rs"],
            labels=["release:patch"],
            release_intent="major",
        )
        patch = self.gate(
            event_name="controller",
            relevant_paths=["src/lib.rs"],
            labels=["release:major"],
            release_intent="patch",
        )
        self.assertEqual(major["release_intent"], "major")
        self.assertEqual(patch["release_intent"], "patch")

    def test_missing_base_and_prerelease_fail_closed(self) -> None:
        self.assertTrue(self.gate(base_version=None)["failures"])
        with self.assertRaises(policy.TrainPolicyError):
            self.gate(version="0.1.1-rc.1")

    def test_semver_zero_to_one_and_no_leading_zero(self) -> None:
        self.assertEqual(
            str(policy.StableVersion.parse("0.9.9").bump("major")),
            "1.0.0",
        )
        with self.assertRaises(policy.TrainPolicyError):
            policy.StableVersion.parse("01.0.0")

    def test_semver_numeric_authority_is_ascii_only(self) -> None:
        for version in ("1.2٢.3", "1.2.3-1٢"):
            with self.subTest(version=version):
                with self.assertRaises(policy.TrainPolicyError):
                    policy.semver_precedence(version)

    def test_registry_precedence_accepts_hyphens_and_build_metadata(self) -> None:
        self.assertLess(
            policy.semver_precedence("1.2.3-alpha-1+build.7"),
            policy.semver_precedence("1.2.3"),
        )
        with self.assertRaises(policy.TrainPolicyError):
            policy.semver_precedence("1.2.3-alpha..1")

    def test_queue_ref_shape_is_exact_and_default_branch_derived(self) -> None:
        exact = f"gh-readonly-queue/main/pr-20-{'f' * 40}"
        self.assertTrue(policy.queue_validation_ref("branch", exact, "main"))
        self.assertTrue(
            policy.queue_validation_ref(
                "branch", f"gh-readonly-queue/main/pr-3-{'a' * 64}", "main"
            )
        )
        rejected = (
            ("branch", exact, "release"),
            ("branch", exact, ""),
            ("tag", exact, "main"),
            ("branch", "", "main"),
            ("branch", f"gh-readonly-queue/main/pr-20-{'f' * 39}", "main"),
            ("branch", f"gh-readonly-queue/main/pr-0-{'f' * 40}", "main"),
            ("branch", f"x/gh-readonly-queue/main/pr-20-{'f' * 40}", "main"),
            ("branch", f"{exact}/extra", "main"),
        )
        for ref_type, ref_name, default_branch in rejected:
            with self.subTest(ref_name=ref_name, default_branch=default_branch):
                self.assertFalse(
                    policy.queue_validation_ref(
                        ref_type, ref_name, default_branch
                    )
                )

    def test_queue_run_holds_no_version_authority_and_never_ejects(self) -> None:
        # The generated train PR moves the version, and the queue validates it
        # on a ref whose range is the whole group. The gate must neither grant
        # authority there nor refuse to classify the event.
        result = self.gate(
            event_name="merge_group",
            ref_name=f"gh-readonly-queue/main/pr-20-{'f' * 40}",
            version="0.1.1",
            changed_paths=["Cargo.toml", "Cargo.lock"],
            relevant_paths=["src/lib.rs"],
        )
        self.assertEqual(result["failures"], [])
        self.assertFalse(result["release_candidate"])
        self.assertFalse(result["trusted_train_pr"])

    def test_merge_group_off_the_exact_queue_ref_fails(self) -> None:
        for ref_name in ("main", f"gh-readonly-queue/main/pr-20-{'f' * 39}"):
            with self.subTest(ref_name=ref_name):
                result = self.gate(event_name="merge_group", ref_name=ref_name)
                self.assertTrue(
                    any(
                        "merge-queue ref" in item
                        for item in result["failures"]
                    )
                )

    def test_unknown_events_still_lose_version_authority(self) -> None:
        result = self.gate(event_name="workflow_dispatch")
        self.assertTrue(
            any(
                "does not grant version authority" in item
                for item in result["failures"]
            )
        )


if __name__ == "__main__":
    unittest.main()
