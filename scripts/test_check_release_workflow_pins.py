"""Adversarial tests for caller Release workflow action pins."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("check-release-workflow-pins.py")
SPEC = importlib.util.spec_from_file_location(
    "check_release_workflow_pins",
    SCRIPT,
)
assert SPEC and SPEC.loader
pins = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pins)

SHA = "a" * 40


class ReleaseWorkflowPinTests(unittest.TestCase):
    def validate(self, text: str) -> int:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.yml"
            path.write_text(text, encoding="utf-8")
            return pins.validate_workflow(path)

    def test_full_sha_actions_are_admitted(self) -> None:
        count = self.validate(
            f"""
jobs:
  release:
    uses: kinlab-ai/kin-actions/.github/workflows/release.yml@{SHA}
    steps:
      - uses: actions/checkout@{SHA} # immutable
"""
        )
        self.assertEqual(count, 2)

    def test_mutable_tag_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            pins.WorkflowPinError,
            "not pinned",
        ):
            self.validate("steps:\n  - uses: actions/checkout@v6\n")

    def test_mutable_toolchain_channel_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            pins.WorkflowPinError,
            "not pinned",
        ):
            self.validate("steps:\n  - uses: dtolnay/rust-toolchain@stable\n")

    def test_whitespace_before_colon_cannot_hide_mutable_ref(self) -> None:
        for key in ("uses ", '"uses" ', "'uses' "):
            with self.subTest(key=key):
                with self.assertRaisesRegex(
                    pins.WorkflowPinError,
                    "not pinned",
                ):
                    self.validate(
                        f"steps:\n  - {key}: actions/checkout@v6\n"
                    )

    def test_semantic_yaml_key_forms_cannot_hide_mutable_ref(self) -> None:
        documents = (
            "steps:\n  - ? uses\n    : actions/checkout@v6\n",
            'steps:\n  - "\\x75ses": actions/checkout@v6\n',
            'steps:\n  - "\\u0075ses": actions/checkout@v6\n',
            "steps:\n  - !!str uses: actions/checkout@v6\n",
            (
                "steps:\n"
                "  - ? >-\n"
                "      uses\n"
                "    : actions/checkout@v6\n"
            ),
            (
                "action_key: &action_key uses\n"
                "steps:\n"
                "  - *action_key: actions/checkout@v6\n"
            ),
        )
        for document in documents:
            with self.subTest(document=document):
                with self.assertRaisesRegex(
                    pins.WorkflowPinError,
                    "not pinned",
                ):
                    self.validate(document)

    def test_duplicate_semantic_uses_keys_are_all_validated(self) -> None:
        with self.assertRaisesRegex(
            pins.WorkflowPinError,
            "not pinned",
        ):
            self.validate(
                "steps:\n"
                "  - uses: actions/checkout@v6\n"
                f"    uses: actions/checkout@{SHA}\n"
            )

    def test_duplicate_anchor_cannot_rewrite_an_earlier_mutable_alias(self) -> None:
        with self.assertRaisesRegex(
            pins.WorkflowPinError,
            "duplicate YAML anchor",
        ):
            self.validate(
                "env:\n"
                "  FIRST: &pin actions/checkout@v6\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: *pin\n"
                "      - run: echo ok\n"
                "        env:\n"
                f"          SECOND: &pin actions/checkout@{SHA}\n"
                "      - uses: *pin\n"
            )

    def test_duplicate_anchor_cannot_rewrite_a_later_mutable_alias(self) -> None:
        with self.assertRaisesRegex(
            pins.WorkflowPinError,
            "duplicate YAML anchor",
        ):
            self.validate(
                "env:\n"
                f"  FIRST: &pin actions/checkout@{SHA}\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: *pin\n"
                "      - run: echo ok\n"
                "        env:\n"
                "          SECOND: &pin actions/checkout@v6\n"
                "      - uses: *pin\n"
            )

    def test_alias_must_resolve_to_a_preceding_scalar(self) -> None:
        with self.assertRaisesRegex(
            pins.WorkflowPinError,
            "preceding scalar anchor",
        ):
            self.validate(
                "steps:\n"
                "  - uses: *pin\n"
                f"pin: &pin actions/checkout@{SHA}\n"
            )

    def test_expression_reference_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            pins.WorkflowPinError,
            "not pinned",
        ):
            self.validate(
                "steps:\n"
                "  - uses: actions/checkout@${{ github.event.ref }}\n"
            )

    def test_nonstandard_uses_surface_fails_closed(self) -> None:
        with self.assertRaisesRegex(pins.WorkflowPinError, "not pinned"):
            self.validate(
                "steps:\n  - { uses: actions/checkout@v6 }\n"
            )

    def test_local_action_indirection_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            pins.WorkflowPinError,
            "transitive external uses cannot be proven",
        ):
            self.validate("steps:\n  - uses: ./.github/actions/local\n")

    def test_missing_workflow_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            pins.WorkflowPinError,
            "missing",
        ):
            pins.validate_workflow(Path("/definitely/missing/release.yml"))


if __name__ == "__main__":
    unittest.main()
