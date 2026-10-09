"""Falsifiers for the version-bound workflow-pin pilot and rollout."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("workflow-pin-rollout.py")
SPEC = importlib.util.spec_from_file_location("workflow_pin_rollout", SCRIPT)
rollout = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = rollout
SPEC.loader.exec_module(rollout)

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / ".kin-release" / "consumers.json"


EXPECTED_INVENTORY = {
    "kinlab-ai/kin": 3,
    "kinlab-ai/kin-bench": 4,
    "kinlab-ai/kinlab": 3,
    "kinlab-ai/kin-blobs": 2,
    "kinlab-ai/kin-search": 2,
    "kinlab-ai/kin-vector": 2,
    "kinlab-ai/kin-infer": 2,
    "kinlab-ai/kin-model": 4,
    "kinlab-ai/kin-db": 3,
    "kinlab-ai/kin-vfs": 3,
    "kinlab-ai/kin-lsp": 3,
    "kinlab-ai/kin-bench-spec": 1,
    "kinlab-ai/kin-editor": 1,
    "kinlab-ai/kin-infra": 1,
    "kinlab-ai/homebrew-kin": 1,
}


class WorkflowPinRolloutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = rollout.load_manifest(MANIFEST)

    def write_manifest(self, data: dict, root: Path) -> Path:
        path = root / "consumers.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_manifest_is_exact_15_repository_35_path_inventory(self) -> None:
        actual = {
            repository: len(spec["workflow_paths"])
            for repository, spec in self.manifest["repositories"].items()
        }
        self.assertEqual(actual, EXPECTED_INVENTORY)
        self.assertEqual(sum(actual.values()), 35)

    def test_one_repository_pilot_and_bottom_up_order_are_exact(self) -> None:
        sequence = rollout.rollout_sequence(self.manifest)
        self.assertEqual(sequence[0]["repository"], "kinlab-ai/kin-blobs")
        self.assertEqual(sequence[0]["stage"], "pilot")
        self.assertEqual(
            [item["repository"] for item in sequence[:8]],
            [
                "kinlab-ai/kin-blobs",
                "kinlab-ai/kin-search",
                "kinlab-ai/kin-vector",
                "kinlab-ai/kin-infer",
                "kinlab-ai/kin-model",
                "kinlab-ai/kin-db",
                "kinlab-ai/kin-vfs",
                "kinlab-ai/kin-lsp",
            ],
        )
        self.assertEqual(
            {item["repository"] for item in sequence[8:]},
            set(EXPECTED_INVENTORY) - rollout.CARGO_REPOSITORIES,
        )
        bootstrap = json.loads(
            (ROOT / ".kin-release/cargo-train-bootstrap.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            {caller["repository"] for caller in bootstrap["callers"]},
            rollout.CARGO_REPOSITORIES,
        )

    def test_unknown_keys_duplicate_members_and_inventory_loss_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cases = []
            unknown = copy.deepcopy(self.manifest)
            unknown["surprise"] = True
            cases.append(unknown)
            duplicate = copy.deepcopy(self.manifest)
            duplicate["rollout"][1]["repositories"].append(
                "kinlab-ai/kin-blobs"
            )
            cases.append(duplicate)
            lost = copy.deepcopy(self.manifest)
            lost["repositories"].pop("kinlab-ai/kin-editor")
            lost["rollout"][-1]["repositories"].remove("kinlab-ai/kin-editor")
            cases.append(lost)
            for index, case in enumerate(cases):
                with self.subTest(index=index), self.assertRaises(
                    rollout.RolloutError
                ):
                    rollout.load_manifest(self.write_manifest(case, root))

    def test_runtime_rejects_substituted_paths_and_reordered_consumers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            changed_path = copy.deepcopy(self.manifest)
            changed_path["repositories"]["kinlab-ai/kin-editor"]["workflow_paths"] = [
                ".github/workflows/other.yml"
            ]
            reordered = copy.deepcopy(self.manifest)
            members = reordered["rollout"][-1]["repositories"]
            members[0], members[1] = members[1], members[0]
            for manifest in (changed_path, reordered):
                with self.subTest(manifest=manifest), self.assertRaises(
                    rollout.RolloutError
                ):
                    rollout.load_manifest(self.write_manifest(manifest, root))

    def inventory(self, target_through: int) -> dict:
        sequence = rollout.rollout_sequence(self.manifest)
        return {
            "target_version": "0.1.34",
            "repositories": [
                {
                    **item,
                    "current_version": "0.1.34" if index < target_through else "0.1.33",
                    "relation": "target" if index < target_through else "behind",
                }
                for index, item in enumerate(sequence)
            ],
        }

    def test_no_later_repository_is_selected_before_pilot_main_proof(self) -> None:
        inventory = self.inventory(1)
        waiting = rollout.plan_rollout(self.manifest, inventory, {})
        self.assertEqual(waiting["status"], "waiting-main-proof")
        self.assertEqual(waiting["repository"], "kinlab-ai/kin-blobs")
        ready = rollout.plan_rollout(
            self.manifest,
            inventory,
            {"kinlab-ai/kin-blobs": "proven"},
        )
        self.assertEqual(ready["status"], "reconcile")
        self.assertEqual(ready["repository"], "kinlab-ai/kin-search")

    def test_each_cargo_main_proof_gates_the_next_bottom_up_repository(self) -> None:
        inventory = self.inventory(6)
        proofs = {
            item["repository"]: "proven"
            for item in rollout.rollout_sequence(self.manifest)[:5]
        }
        waiting = rollout.plan_rollout(self.manifest, inventory, proofs)
        self.assertEqual(waiting["repository"], "kinlab-ai/kin-db")
        self.assertEqual(waiting["status"], "waiting-main-proof")
        proofs["kinlab-ai/kin-db"] = "proven"
        next_plan = rollout.plan_rollout(self.manifest, inventory, proofs)
        self.assertEqual(next_plan["repository"], "kinlab-ai/kin-vfs")

    def test_each_non_cargo_landing_gates_the_next_repository(self) -> None:
        sequence = rollout.rollout_sequence(self.manifest)
        inventory = self.inventory(9)
        proofs = {item["repository"]: "proven" for item in sequence[:8]}
        waiting = rollout.plan_rollout(self.manifest, inventory, proofs)
        self.assertEqual(waiting["repository"], "kinlab-ai/kin")
        self.assertEqual(waiting["status"], "waiting-landing-proof")
        proofs["kinlab-ai/kin"] = "proven"
        next_plan = rollout.plan_rollout(self.manifest, inventory, proofs)
        self.assertEqual(next_plan["repository"], "kinlab-ai/kin-bench")
        self.assertEqual(next_plan["status"], "reconcile")

    def make_checkouts(self, root: Path, version: str = "0.1.33") -> None:
        for repository, spec in self.manifest["repositories"].items():
            checkout = rollout.checkout_directory(root, repository)
            for relative in spec["workflow_paths"]:
                path = checkout / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "jobs:\n"
                    "  use:\n"
                    "    uses: kinlab-ai/kin-actions/.github/workflows/"
                    f"example.yml@v{version}\n",
                    encoding="utf-8",
                )

    def test_global_preflight_finds_last_repository_drift_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.make_checkouts(root)
            last = rollout.checkout_directory(root, "kinlab-ai/homebrew-kin")
            extra = last / ".github/workflows/unmanifested.yml"
            extra.write_text(
                "jobs:\n  use:\n    uses: kinlab-ai/kin-actions/.github/"
                "workflows/example.yml@v0.1.33\n",
                encoding="utf-8",
            )
            before = {
                path: path.read_bytes() for path in root.rglob("*.yml")
            }
            with self.assertRaisesRegex(
                rollout.RolloutError, "homebrew-kin: manifest inventory drift"
            ):
                rollout.preflight_checkouts(self.manifest, root, "0.1.34")
            self.assertEqual(
                {path: path.read_bytes() for path in root.rglob("*.yml")}, before
            )

    def test_global_preflight_rejects_a_late_unrewritable_pin_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.make_checkouts(root)
            last = rollout.checkout_directory(root, "kinlab-ai/homebrew-kin")
            path = last / ".github/workflows/ci.yml"
            path.write_text(
                "jobs:\n"
                "  use:\n"
                '    uses: "kinlab-ai/kin-actions/.github/workflows/'
                'example.yml@v0.1.33"\n',
                encoding="utf-8",
            )
            before = {candidate: candidate.read_bytes() for candidate in root.rglob("*.yml")}
            with self.assertRaisesRegex(
                rollout.RolloutError, "pin rewrite preflight failed"
            ):
                rollout.preflight_checkouts(self.manifest, root, "0.1.34")
            self.assertEqual(
                {candidate: candidate.read_bytes() for candidate in root.rglob("*.yml")},
                before,
            )

    def test_sha_pinned_consumer_is_an_activation_hold(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.make_checkouts(root)
            path = (
                rollout.checkout_directory(root, "kinlab-ai/kin-infra")
                / ".github/workflows/ci.yml"
            )
            path.write_text(
                "jobs:\n  use:\n    uses: kinlab-ai/kin-actions/.github/"
                "workflows/example.yml@fc3421c343b8f9157bdc7daa9c5a2aee75809fd8\n",
                encoding="utf-8",
            )
            inventory = rollout.preflight_checkouts(
                self.manifest, root, "0.1.34"
            )
            state = next(
                item
                for item in inventory["repositories"]
                if item["repository"] == "kinlab-ai/kin-infra"
            )
            self.assertEqual(state["relation"], "blocked")
            self.assertIn("stable version tag", state["blocker"])

    def test_late_activation_hold_does_not_suppress_the_versioned_pilot(self) -> None:
        inventory = self.inventory(0)
        inventory["repositories"][-1] = {
            **inventory["repositories"][-1],
            "relation": "blocked",
            "blocker": "later policy migration",
        }
        pilot = rollout.plan_rollout(self.manifest, inventory, {})
        self.assertEqual(pilot["status"], "reconcile")
        self.assertEqual(pilot["repository"], "kinlab-ai/kin-blobs")

        for item in inventory["repositories"]:
            if item["repository"] != "kinlab-ai/homebrew-kin":
                item["relation"] = "target"
        proofs = {
            item["repository"]: "proven"
            for item in inventory["repositories"][:-1]
        }
        blocked = rollout.plan_rollout(self.manifest, inventory, proofs)
        self.assertEqual(blocked["status"], "blocked-activation")
        self.assertEqual(blocked["repository"], "kinlab-ai/homebrew-kin")

    def test_controller_preflights_globally_and_proves_before_arming(self) -> None:
        controller = (ROOT / "scripts/run-workflow-pin-wave.sh").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            controller.index('"$planner" preflight'),
            controller.index('reconcile_output="$('),
        )
        self.assertLess(
            controller.index('python3 "$protection_proof"'),
            controller.index('reconcile_output="$('),
        )
        self.assertGreater(
            controller.index('python3 "$protection_proof"'),
            controller.index('python3 "$planner" plan'),
        )
        self.assertLess(
            controller.index('"$landing_proof"'),
            controller.index('"$planner" plan'),
        )
        self.assertLess(
            controller.index('"$main_proof"'),
            controller.index('"$planner" plan'),
        )
        self.assertLess(
            controller.index('"$pr_proof"'),
            controller.index('gh pr merge "$pr"'),
        )
        self.assertEqual(controller.count('"$reconciler"'), 2)
        self.assertIn("auto-merge remains off", controller)


if __name__ == "__main__":
    unittest.main()
