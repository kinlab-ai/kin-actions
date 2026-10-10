"""Tests for live consumer protection and required-check provenance."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("workflow-pin-protection.py")
SPEC = importlib.util.spec_from_file_location("workflow_pin_protection", SCRIPT)
protection = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = protection
SPEC.loader.exec_module(protection)


class WorkflowPinProtectionTests(unittest.TestCase):
    repository = "kinlab-ai/kin-blobs"

    def settings(self, **overrides) -> dict:
        value = {
            "full_name": self.repository,
            "default_branch": "main",
            **protection.EXPECTED_SETTINGS,
        }
        value.update(overrides)
        return value

    def checks(self, contexts=None, *, strict=True) -> dict:
        contexts = sorted(contexts or protection.CARGO_CONTEXTS)
        return {
            "strict": strict,
            "contexts": contexts,
            "checks": [
                {"context": context, "app_id": 15368} for context in contexts
            ],
        }

    def validate(self, **overrides):
        values = {
            "repository_settings": self.settings(),
            "status_checks": self.checks(),
            "repository": self.repository,
            "kind": "cargo_release",
            "required_app_id": 15368,
        }
        values.update(overrides)
        return protection.validate_protection(**values)

    def test_exact_strict_app_bound_contract_passes(self) -> None:
        result = self.validate()
        self.assertEqual(result["default_branch"], "main")
        self.assertEqual(set(result["required_checks"]), protection.CARGO_CONTEXTS)

    def test_merge_settings_strictness_and_empty_checks_fail_closed(self) -> None:
        with self.assertRaisesRegex(protection.ProtectionError, "allow_auto_merge"):
            self.validate(repository_settings=self.settings(allow_auto_merge=False))
        with self.assertRaisesRegex(protection.ProtectionError, "strict/up-to-date"):
            self.validate(status_checks=self.checks(strict=False))
        with self.assertRaisesRegex(protection.ProtectionError, "at least one"):
            self.validate(status_checks={"strict": True, "contexts": [], "checks": []})

    def test_missing_unbound_duplicate_and_wrong_app_checks_fail_closed(self) -> None:
        missing = self.checks(protection.CARGO_CONTEXTS - {"release / Repo verification"})
        with self.assertRaisesRegex(protection.ProtectionError, "missing Cargo"):
            self.validate(status_checks=missing)
        unbound = self.checks()
        unbound["checks"][0]["app_id"] = None
        with self.assertRaisesRegex(protection.ProtectionError, "not App-bound"):
            self.validate(status_checks=unbound)
        duplicate = self.checks()
        duplicate["checks"].append(dict(duplicate["checks"][0]))
        with self.assertRaisesRegex(protection.ProtectionError, "duplicate"):
            self.validate(status_checks=duplicate)
        wrong = self.checks()
        wrong["checks"][0]["app_id"] = 1
        with self.assertRaisesRegex(protection.ProtectionError, "wrong App"):
            self.validate(status_checks=wrong)

    def test_other_consumer_may_use_a_different_exact_app_bound_context(self) -> None:
        status = {
            "strict": True,
            "contexts": ["policy"],
            "checks": [{"context": "policy", "app_id": 42}],
        }
        result = self.validate(status_checks=status, kind="other")
        self.assertEqual(result["required_checks"], {"policy": 42})


if __name__ == "__main__":
    unittest.main()
