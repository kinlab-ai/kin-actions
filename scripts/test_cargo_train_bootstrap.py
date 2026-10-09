"""Contracts for the safe two-release Cargo train activation."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_RELEASE_CONTEXTS = [
    "release / Version bump gate",
    "release / Registry-only build",
    "release / Repo verification",
]
GITHUB_ACTIONS_APP_ID = 15368


class CargoTrainBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bootstrap = json.loads(
            (ROOT / ".kin-release/cargo-train-bootstrap.json").read_text(
                encoding="utf-8"
            )
        )
        cls.consumers = json.loads(
            (ROOT / ".kin-release/consumers.json").read_text(
                encoding="utf-8"
            )
        )
        cls.template = (
            ROOT / ".kin-release/release-train-caller.yml"
        ).read_text(encoding="utf-8")

    def test_exact_eight_caller_package_and_manifest_contracts(self) -> None:
        rows = self.bootstrap["callers"]
        self.assertEqual(self.bootstrap["schema"], 2)
        self.assertEqual(len(rows), 8)
        self.assertEqual(
            self.bootstrap["repository_merge_contract"],
            {
                "allow_auto_merge": True,
                "allow_squash_merge": True,
                "allow_merge_commit": False,
                "allow_rebase_merge": False,
                "squash_merge_commit_title": "PR_TITLE",
                "squash_merge_commit_message": "PR_BODY",
            },
        )
        expected = {
            "kinlab-ai/kin-blobs": ("kin-blobs", "package"),
            "kinlab-ai/kin-search": ("kin-search", "package"),
            "kinlab-ai/kin-vector": ("kin-vector", "package"),
            "kinlab-ai/kin-infer": ("kin-infer", "package"),
            "kinlab-ai/kin-model": ("kin-model", "package"),
            "kinlab-ai/kin-db": ("kin-db", "workspace.package"),
            "kinlab-ai/kin-lsp": ("kin-lsp", "package"),
            "kinlab-ai/kin-vfs": ("kin-vfs-core", "workspace.package"),
        }
        self.assertEqual(
            {
                row["repository"]: (
                    row["package"],
                    row["version_authority"],
                )
                for row in rows
            },
            expected,
        )
        self.assertTrue(all(row["manifest"] == "Cargo.toml" for row in rows))
        for row in rows:
            self.assertEqual(
                row["required_main_status_checks"],
                [
                    {
                        "context": context,
                        "app_id": GITHUB_ACTIONS_APP_ID,
                    }
                    for context in REQUIRED_RELEASE_CONTEXTS
                ],
            )
            self.assertEqual(
                row["release_workflow_path"],
                ".github/workflows/release.yml",
            )
            self.assertEqual(
                row["squash_merge_commit_message"],
                "PR_BODY",
            )

    def test_four_dependency_callers_disable_own_version_bumps(self) -> None:
        configured = {
            row["repository"]: row["dependency_wave"]
            for row in self.bootstrap["callers"]
            if row["dependency_wave"] is not None
        }
        self.assertEqual(
            set(configured),
            {
                "kinlab-ai/kin-model",
                "kinlab-ai/kin-db",
                "kinlab-ai/kin-lsp",
                "kinlab-ai/kin-vfs",
            },
        )
        for contract in configured.values():
            self.assertEqual(
                contract["path"],
                ".github/workflows/kin-dependency-wave.yml",
            )
            self.assertEqual(contract["version_mode"], "train")
            self.assertIs(contract["bump_own_version"], False)
            self.assertEqual(contract["pin"], "release_a")
            self.assertEqual(contract["secrets"], "inherit")

    def test_registry_and_combined_caller_contracts_pin_a_and_inherit_app(self) -> None:
        registry = self.bootstrap["registry_workflow_contract"]
        self.assertEqual(
            registry,
            {
                "path": ".github/workflows/registry-publish.yml",
                "pin": "release_a",
                "version_mode": "train",
                "mint_release_tag": True,
                "job_id": "release",
                "secrets": "inherit",
            },
        )
        caller = self.bootstrap["release_train_caller_contract"]
        self.assertEqual(
            caller["path"], self.bootstrap["future_caller_path"]
        )
        self.assertEqual(caller["pin"], "release_a")
        self.assertEqual(caller["secrets"], "inherit")
        self.assertEqual(caller["release_workflow"], "Release")
        self.assertEqual(
            caller["required_check_recovery"],
            (
                "branch-required+github-actions-app-15368+exact-head+"
                "same-repo+pull-request"
            ),
        )
        self.assertEqual(caller["recovery_actions_permission"], "write")
        self.assertEqual(caller["recovery_checks_permission"], "read")
        self.assertEqual(
            caller["recovery_pull_requests_permission"],
            "read",
        )
        self.assertEqual(caller["terminal_issue_permission"], "write")
        self.assertEqual(caller["controller_window_seconds"], 2700)
        self.assertEqual(caller["terminal_reserve_seconds"], 600)
        self.assertEqual(
            self.bootstrap["release_app_repository_permissions"],
            {
                "administration": "read",
                "contents": "write",
                "pull_requests": "write",
                "issues": "write",
                "actions": "none",
                "workflows": "none",
            },
        )
        self.assertEqual(
            self.bootstrap["caller_release_workflow_contract"],
            {
                "path": ".github/workflows/release.yml",
                "external_uses_ref": "40-lowercase-hex-sha",
                "local_uses_allowed": False,
                "enforced_by": "scripts/check-release-workflow-pins.py",
            },
        )
        self.assertEqual(
            set(caller["automatic_triggers"]),
            {
                "ci_workflow_run",
                "registry_publish_workflow_run",
                "repository_dispatch",
                "schedule",
            },
        )

    def test_future_caller_paths_are_deliberately_not_yet_in_inventory(self) -> None:
        future = self.bootstrap["future_caller_path"]
        for row in self.bootstrap["callers"]:
            self.assertNotIn(
                future,
                self.consumers["repositories"][row["repository"]],
            )

    def test_combined_caller_has_automatic_train_and_recovery_triggers(self) -> None:
        self.assertNotIn("workflow_dispatch:", self.template)
        self.assertIn('workflows: ["CI", "Registry Publish"]', self.template)
        self.assertIn("repository_dispatch:", self.template)
        self.assertIn("schedule:", self.template)
        self.assertIn(
            ".github/workflows/cargo-release-train.yml@vRELEASE_A",
            self.template,
        )
        self.assertIn(
            ".github/workflows/cargo-release-recovery.yml@vRELEASE_A",
            self.template,
        )
        self.assertEqual(self.template.count("secrets: inherit"), 2)
        self.assertIn("release-workflow: \"Release\"", self.template)
        self.assertEqual(self.template.count("actions: write"), 2)
        self.assertEqual(self.template.count("checks: read"), 1)
        self.assertEqual(self.template.count("issues: write"), 2)
        self.assertEqual(self.template.count("pull-requests: read"), 1)


if __name__ == "__main__":
    unittest.main()
