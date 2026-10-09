"""Unit tests for exact landed-main Cargo rollout proof."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("check-workflow-pin-main.py")
SPEC = importlib.util.spec_from_file_location("check_workflow_pin_main", SCRIPT)
proof = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = proof
SPEC.loader.exec_module(proof)


class WorkflowPinMainProofTests(unittest.TestCase):
    sha = "a" * 40

    def workflow_run(self, **overrides):
        value = {
            "id": 42,
            "head_sha": self.sha,
            "head_branch": "main",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "repository": {"full_name": "kinlab-ai/kin-blobs"},
            "head_repository": {"full_name": "kinlab-ai/kin-blobs"},
        }
        value.update(overrides)
        return value

    def jobs(self):
        return [
            {"name": name, "conclusion": conclusion}
            for name, conclusion in proof.EXPECTED_JOBS.items()
        ]

    def test_all_four_release_jobs_prove_exact_main(self) -> None:
        result = proof.evaluate_main_proof(
            repository="kinlab-ai/kin-blobs",
            branch="main",
            head_sha=self.sha,
            runs=[self.workflow_run()],
            jobs_by_run={42: self.jobs()},
        )
        self.assertEqual(result["status"], "proven")

    def test_no_run_and_incomplete_run_wait(self) -> None:
        self.assertEqual(
            proof.evaluate_main_proof(
                repository="kinlab-ai/kin-blobs",
                branch="main",
                head_sha=self.sha,
                runs=[],
                jobs_by_run={},
            )["status"],
            "waiting",
        )
        self.assertEqual(
            proof.evaluate_main_proof(
                repository="kinlab-ai/kin-blobs",
                branch="main",
                head_sha=self.sha,
                runs=[self.workflow_run(status="in_progress", conclusion=None)],
                jobs_by_run={42: []},
            )["status"],
            "waiting",
        )

    def test_wrong_event_sha_branch_or_repository_cannot_prove(self) -> None:
        for run in (
            self.workflow_run(event="pull_request"),
            self.workflow_run(head_sha="b" * 40),
            self.workflow_run(head_branch="automation/spoof"),
            self.workflow_run(repository={"full_name": "attacker/fork"}),
            self.workflow_run(head_repository={"full_name": "attacker/fork"}),
        ):
            with self.subTest(run=run):
                self.assertEqual(
                    proof.evaluate_main_proof(
                        repository="kinlab-ai/kin-blobs",
                        branch="main",
                        head_sha=self.sha,
                        runs=[run],
                        jobs_by_run={42: self.jobs()},
                    )["status"],
                    "waiting",
                )

    def test_missing_or_wrong_release_job_fails_closed(self) -> None:
        missing = self.jobs()[:-1]
        with self.assertRaisesRegex(proof.MainProofError, "lacks release jobs"):
            proof.evaluate_main_proof(
                repository="kinlab-ai/kin-blobs",
                branch="main",
                head_sha=self.sha,
                runs=[self.workflow_run()],
                jobs_by_run={42: missing},
            )
        wrong = self.jobs()
        wrong[-1]["conclusion"] = "success"
        with self.assertRaisesRegex(proof.MainProofError, "conclusions differ"):
            proof.evaluate_main_proof(
                repository="kinlab-ai/kin-blobs",
                branch="main",
                head_sha=self.sha,
                runs=[self.workflow_run()],
                jobs_by_run={42: wrong},
            )


if __name__ == "__main__":
    unittest.main()
