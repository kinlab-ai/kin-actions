"""Tests for the rollout's no-bypass tag-freeze activation gate."""

from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("check-workflow-pin-activation.py")
ROOT = SCRIPT.parent.parent
SPEC = importlib.util.spec_from_file_location("check_workflow_pin_activation", SCRIPT)
activation = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = activation
SPEC.loader.exec_module(activation)


class WorkflowPinActivationTests(unittest.TestCase):
    def valid(self) -> dict:
        return {
            "id": activation.EXPECTED_RULESET_ID,
            "source": "kinlab-ai/kin-actions",
            "source_type": "Repository",
            "name": "Freeze version release tags",
            "target": "tag",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": {
                "ref_name": {
                    "exclude": [],
                    "include": ["refs/tags/v*.*.*"],
                }
            },
            "rules": [
                {"type": "update"},
                {"type": "deletion"},
                {"type": "non_fast_forward"},
            ],
        }

    def test_exact_no_bypass_freeze_is_ready(self) -> None:
        self.assertEqual(activation.validate_tag_freeze(self.valid())["status"], "ready")

    def test_user_bypass_and_missing_rule_fail_closed(self) -> None:
        bypass = self.valid()
        bypass["bypass_actors"] = [
            {"actor_type": "User", "actor_id": 63249686, "bypass_mode": "always"}
        ]
        with self.assertRaisesRegex(activation.ActivationError, "zero bypass"):
            activation.validate_tag_freeze(bypass)
        missing = copy.deepcopy(self.valid())
        missing["rules"].pop()
        with self.assertRaisesRegex(activation.ActivationError, "must block"):
            activation.validate_tag_freeze(missing)

    def test_read_only_response_cannot_claim_zero_bypass_actors(self) -> None:
        hidden = self.valid()
        hidden.pop("bypass_actors")
        with self.assertRaisesRegex(
            activation.ActivationError, "external ruleset-write audit"
        ):
            activation.validate_tag_freeze(hidden)

    def test_exact_ruleset_identity_source_and_multiplicity_are_required(self) -> None:
        wrong_id = self.valid()
        wrong_id["id"] += 1
        with self.assertRaisesRegex(activation.ActivationError, "ruleset id"):
            activation.validate_tag_freeze(wrong_id)

        wrong_source = self.valid()
        wrong_source["source"] = "firelock-ai/other"
        with self.assertRaisesRegex(activation.ActivationError, "exact repository"):
            activation.validate_tag_freeze(wrong_source)

        duplicate = self.valid()
        duplicate["rules"].append({"type": "update"})
        with self.assertRaisesRegex(activation.ActivationError, "must block"):
            activation.validate_tag_freeze(duplicate)

    def test_candidate_workflow_holds_without_receiving_ruleset_write(self) -> None:
        workflow = (ROOT / ".github/workflows/pin-wave.yml").read_text(
            encoding="utf-8"
        )
        hold = workflow.index("Hold for external no-bypass tag-freeze audit")
        rollout = workflow.index("Globally preflight and advance one proof-gated consumer")
        self.assertLess(hold, rollout)
        held_step = workflow[hold:rollout]
        self.assertIn("external ruleset-write audit", held_step)
        self.assertIn("separately provisioned ruleset-write credential", held_step)
        self.assertIn("exit 1", held_step)
        self.assertNotIn("KIN_RULESET_AUDIT_TOKEN", workflow)
        self.assertNotIn("permission-administration: write", workflow)
        self.assertNotIn("check-workflow-pin-activation.py", workflow)


if __name__ == "__main__":
    unittest.main()
