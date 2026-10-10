"""Unit tests for exact-head workflow-pin PR admission."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("check-workflow-pin-pr.py")
SPEC = importlib.util.spec_from_file_location("check_workflow_pin_pr", SCRIPT)
admission = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = admission
SPEC.loader.exec_module(admission)


class WorkflowPinAdmissionTests(unittest.TestCase):
    sha = "a" * 40

    def pr(self, sha: str | None = None) -> dict:
        return {
            "state": "open",
            "draft": False,
            "head": {
                "sha": sha or self.sha,
                "ref": "automation/kin-actions-pin-next",
                "repo": {"full_name": "kinlab-ai/kin-blobs"},
            },
            "base": {"ref": "main", "sha": "b" * 40},
        }

    def checks(self, conclusion: str = "success", app_id: int = 15368):
        return [
            {
                "name": name,
                "status": "completed",
                "conclusion": conclusion,
                "app": {"id": app_id},
            }
            for name in sorted(admission.CARGO_CONTEXTS)
        ]

    def required(self, bucket: str = "pass"):
        return [
            {"name": name, "bucket": bucket}
            for name in sorted(admission.CARGO_CONTEXTS)
        ]

    def settings(self) -> dict:
        return {
            "full_name": "kinlab-ai/kin-blobs",
            "default_branch": "main",
            **admission.protection_guard.EXPECTED_SETTINGS,
        }

    def status_checks(self, contexts=None) -> dict:
        contexts = sorted(contexts or admission.CARGO_CONTEXTS)
        return {
            "strict": True,
            "contexts": contexts,
            "checks": [
                {"context": context, "app_id": 15368} for context in contexts
            ],
        }

    def evaluate(self, **overrides):
        values = {
            "pr": self.pr(),
            "check_runs": self.checks(),
            "required_checks": self.required(),
            "repository_settings": self.settings(),
            "status_checks": self.status_checks(),
            "repository": "kinlab-ai/kin-blobs",
            "base": "main",
            "base_sha": "b" * 40,
            "head_branch": "automation/kin-actions-pin-next",
            "head_sha": self.sha,
            "kind": "cargo_release",
            "required_app_id": 15368,
        }
        values.update(overrides)
        return admission.evaluate_admission(**values)

    def test_exact_green_head_is_ready(self) -> None:
        self.assertEqual(self.evaluate()["status"], "ready")

    def test_pending_check_waits_without_arming(self) -> None:
        checks = self.checks()
        checks[0]["status"] = "in_progress"
        checks[0]["conclusion"] = None
        self.assertEqual(self.evaluate(check_runs=checks)["status"], "waiting")

    def test_stale_or_foreign_head_fails_closed(self) -> None:
        for pr in (
            self.pr("c" * 40),
            {
                **self.pr(),
                "head": {
                    **self.pr()["head"],
                    "repo": {"full_name": "attacker/fork"},
                },
            },
        ):
            with self.subTest(pr=pr), self.assertRaises(
                admission.AdmissionError
            ):
                self.evaluate(pr=pr)

    def test_advanced_base_fails_closed(self) -> None:
        pr = self.pr()
        pr["base"]["sha"] = "c" * 40
        with self.assertRaisesRegex(admission.AdmissionError, "exact trusted main"):
            self.evaluate(pr=pr)

    def test_failed_cancelled_neutral_and_missing_required_fail_closed(self) -> None:
        for conclusion in ("failure", "cancelled", "neutral", "timed_out"):
            with self.subTest(conclusion=conclusion), self.assertRaises(
                admission.AdmissionError
            ):
                self.evaluate(check_runs=self.checks(conclusion))
        self.assertEqual(
            self.evaluate(required_checks=[])["status"], "waiting"
        )

    def test_wrong_app_and_duplicate_checks_fail_closed(self) -> None:
        with self.assertRaisesRegex(admission.AdmissionError, "wrong app"):
            self.evaluate(check_runs=self.checks(app_id=1))
        duplicate = self.checks()
        duplicate.append(dict(duplicate[0]))
        with self.assertRaisesRegex(admission.AdmissionError, "duplicate"):
            self.evaluate(check_runs=duplicate)

    def test_skipped_cargo_proof_context_fails_closed(self) -> None:
        checks = self.checks()
        checks[0]["conclusion"] = "skipped"
        required = self.required()
        required[0]["bucket"] = "skipping"
        with self.assertRaisesRegex(admission.AdmissionError, "did not pass"):
            self.evaluate(check_runs=checks, required_checks=required)

    def test_other_consumer_uses_complete_full_set_without_required_contexts(self) -> None:
        checks = [
            {
                "name": "CI / test",
                "status": "completed",
                "conclusion": "success",
                "app": {"id": 15368},
            }
        ]
        ready = self.evaluate(
            check_runs=checks,
            required_checks=[{"name": "CI / test", "bucket": "pass"}],
            status_checks={
                "strict": True,
                "contexts": ["CI / test"],
                "checks": [{"context": "CI / test", "app_id": 15368}],
            },
            kind="other",
        )
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["required_checks"], 1)


if __name__ == "__main__":
    unittest.main()
