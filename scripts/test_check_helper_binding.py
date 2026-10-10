"""Unit tests for reusable-workflow helper source binding."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("check-helper-binding.py")
SPEC = importlib.util.spec_from_file_location("check_helper_binding", SCRIPT)
binding = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = binding
SPEC.loader.exec_module(binding)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTROLLERS = (
    ".github/workflows/cargo-release-train.yml",
    ".github/workflows/cargo-release-recovery.yml",
)
SHA = "d23441a48e516b6c34aea4fa41551a30e30af803"


def step(*, name: bool, repository: str, ref: str, path: str = "helper") -> str:
    lines = ["jobs:", "  build:", "    steps:"]
    if name:
        lines.append("      - name: Check out exact called-workflow helpers")
        lines.append("        id: helper_checkout")
        lines.append(f"        uses: actions/checkout@{SHA} # v6.1.0")
    else:
        lines.append(f"      - uses: actions/checkout@{SHA} # v6.1.0")
    lines.append("        with:")
    lines.append(f"          repository: {repository}")
    lines.append(f"          ref: {ref}")
    lines.append(f"          path: {path}")
    return "\n".join(lines) + "\n"


BOUND_REPOSITORY = "${{ job.workflow_repository }}"
BOUND_REF = "${{ job.workflow_sha }}"


class HelperBindingTests(unittest.TestCase):
    def violations(self, text: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.yml"
            path.write_text(text, encoding="utf-8")
            return binding.violations(path)

    def run_main(self, workflows: dict[str, str]) -> int:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".github" / "workflows"
            target.mkdir(parents=True)
            for name, text in workflows.items():
                (target / name).write_text(text, encoding="utf-8")
            return binding.main([str(root)])

    def test_bound_helper_checkout_passes(self) -> None:
        for named in (True, False):
            with self.subTest(named=named):
                text = step(
                    name=named,
                    repository=BOUND_REPOSITORY,
                    ref=BOUND_REF,
                )
                self.assertEqual(self.violations(text), [])
                self.assertEqual(len(binding.checkout_blocks(text)), 1)

    def test_step_declaring_name_before_uses_is_covered(self) -> None:
        text = step(name=True, repository=BOUND_REPOSITORY, ref="main")
        self.assertEqual(len(binding.checkout_blocks(text)), 1)
        self.assertTrue(self.violations(text))

    def test_helper_path_name_cannot_hide_the_checkout(self) -> None:
        for path in (".kin-actions", "kin-actions-helper", "vendor/anything"):
            with self.subTest(path=path):
                text = step(
                    name=True,
                    repository=BOUND_REPOSITORY,
                    ref="main",
                    path=path,
                )
                self.assertEqual(len(binding.checkout_blocks(text)), 1)

    def test_missing_repository_binding_is_flagged(self) -> None:
        text = step(name=True, repository="kinlab-ai/kin-actions", ref=BOUND_REF)
        self.assertIn(
            "helper checkout 1 does not use job.workflow_repository",
            self.violations(text),
        )

    def test_missing_sha_binding_is_flagged(self) -> None:
        text = step(name=True, repository=BOUND_REPOSITORY, ref="v6")
        self.assertIn(
            "helper checkout 1 does not use job.workflow_sha",
            self.violations(text),
        )

    def test_separately_mutable_ref_is_flagged(self) -> None:
        for ref in ("main", "master", "v6"):
            with self.subTest(ref=ref):
                text = step(name=True, repository=BOUND_REPOSITORY, ref=ref)
                self.assertTrue(
                    any("mutable ref" in message for message in self.violations(text))
                )

    def test_deprecated_helper_ref_input_is_flagged(self) -> None:
        text = step(name=True, repository=BOUND_REPOSITORY, ref=BOUND_REF)
        text += "        # ref: ${{ inputs.kin-actions-ref }}\n"
        self.assertIn(
            "uses deprecated kin-actions-ref as helper authority",
            self.violations(text),
        )

    def test_caller_own_checkout_is_not_a_helper(self) -> None:
        text = (
            "jobs:\n"
            "  build:\n"
            "    steps:\n"
            f"      - uses: actions/checkout@{SHA} # v6.1.0\n"
            "        with:\n"
            "          ref: ${{ github.event.repository.default_branch }}\n"
            "          path: caller\n"
        )
        self.assertEqual(binding.checkout_blocks(text), [])

    def test_absent_helper_checkout_fails_closed(self) -> None:
        self.assertEqual(self.run_main({"only.yml": "jobs: {}\n"}), 1)

    def test_violating_workflow_fails_main(self) -> None:
        text = step(name=True, repository=BOUND_REPOSITORY, ref="main")
        self.assertEqual(self.run_main({"controller.yml": text}), 1)

    def test_both_new_controller_paths_are_covered(self) -> None:
        for relative in CONTROLLERS:
            with self.subTest(workflow=relative):
                path = REPO_ROOT / relative
                self.assertTrue(path.is_file(), f"missing controller: {relative}")
                blocks = binding.checkout_blocks(path.read_text(encoding="utf-8"))
                self.assertTrue(
                    blocks,
                    f"{relative} presents no helper checkout to the binding gate",
                )
                self.assertEqual(binding.violations(path), [])

    def test_repository_workflows_pass_the_live_gate(self) -> None:
        self.assertEqual(binding.main([str(REPO_ROOT)]), 0)


if __name__ == "__main__":
    unittest.main()
