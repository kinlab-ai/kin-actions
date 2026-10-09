"""Black-box tests for bounded idempotent Cargo release recovery."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("recover-cargo-release.sh")
HEAD = "a" * 40
MARKER = "<!-- kin-cargo-release:downstreams-dispatched -->"


class CargoReleaseRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.helper = self.root / "helper"
        self.bin.mkdir()
        (self.helper / "scripts").mkdir(parents=True)
        self.output = self.root / "output"
        self.summary = self.root / "summary"
        self.log = self.root / "mutations"
        self.timeout_log = self.root / "timeouts"
        self.release_views = self.root / "release-views"
        self.release_edits = self.root / "release-edits"

        self.executable(
            self.bin / "python3",
            f"""#!/usr/bin/env bash
case "$1" in
  *prepare-cargo-release.py)
    printf '%s\\n' '{{"current_version":"1.2.3"}}'
    ;;
  *resolve-version-commit.py)
    printf '%s\\n' '{HEAD}'
    ;;
  *recover-registry-publish.py)
    if [[ "${{FAKE_PUBLICATION_RECOVERY_FAIL:-}}" == "true" ]]; then
      exit 7
    fi
    printf '%s\\n' available > "$FAKE_REGISTRY_STATE_FILE"
    ;;
  *inspect-registry-version.py)
    state="${{FAKE_REGISTRY_STATE}}"
    if [[ -f "$FAKE_REGISTRY_STATE_FILE" ]]; then
      state="$(cat "$FAKE_REGISTRY_STATE_FILE")"
    fi
    printf '{{"state":"%s"}}\\n' "$state"
    ;;
  *)
    exec {shlex.quote(sys.executable)} "$@"
    ;;
esac
""",
        )
        self.executable(
            self.bin / "git",
            f"""#!/usr/bin/env bash
if [[ "$1" == "rev-parse" && "$2" == "HEAD" ]]; then
  printf '%s\\n' '{HEAD}'
  exit 0
fi
if [[ "$1" == "ls-remote" ]]; then
  if [[ "$FAKE_TAG_PRESENT" == "true" ]]; then
    printf '%s\\t%s\\n' '{HEAD}' 'refs/tags/v1.2.3'
  fi
  exit 0
fi
echo "unexpected git: $*" >&2
exit 9
""",
        )
        self.executable(
            self.bin / "gh",
            f"""#!/usr/bin/env bash
case "$1 $2" in
  "api repos/"*)
    printf '%s\\n' '{HEAD}'
    ;;
  "run list")
    printf '%s\\n' success
    ;;
  "release view")
    count=0
    if [[ -f "$FAKE_RELEASE_VIEWS" ]]; then
      count="$(cat "$FAKE_RELEASE_VIEWS")"
    fi
    count=$((count + 1))
    printf '%s' "$count" > "$FAKE_RELEASE_VIEWS"
    if [[ "$FAKE_RELEASE_INITIAL_ABSENT" == "true" && "$count" == "1" ]]; then
      exit 0
    fi
    printf '%s\\n' "$FAKE_RELEASE_JSON"
    ;;
  "release edit")
    count=0
    if [[ -f "$FAKE_RELEASE_EDITS" ]]; then
      count="$(cat "$FAKE_RELEASE_EDITS")"
    fi
    count=$((count + 1))
    printf '%s' "$count" > "$FAKE_RELEASE_EDITS"
    if [[ "${{FAKE_RELEASE_EDIT_FAIL_AT:-0}}" == "$count" ]]; then
      exit 12
    fi
    printf '%s\\n' release-edit >> "$FAKE_MUTATION_LOG"
    ;;
  *)
    echo "unexpected gh: $*" >&2
    exit 8
    ;;
esac
""",
        )
        self.executable(
            self.bin / "timeout",
            """#!/usr/bin/env bash
if [[ "$1" == "--signal=TERM" ]]; then shift; fi
if [[ "$1" == "--kill-after=5s" ]]; then shift; fi
printf '%s\\n' "$1" >> "$FAKE_TIMEOUT_LOG"
shift
exec "$@"
""",
        )
        self.executable(
            self.bin / "date",
            """#!/usr/bin/env bash
if [[ "$1" != "+%s" ]]; then
  echo "unexpected date: $*" >&2
  exit 9
fi
printf '%s\\n' "$FAKE_NOW_EPOCH"
""",
        )
        for name in (
            "consumer-smoke.sh",
            "dispatch-downstreams.sh",
            "mint-release-tag.sh",
            "wait-tag-release-run.sh",
        ):
            failure = (
                'if [[ "${FAKE_MINT_FAIL:-}" == "true" ]]; then exit 7; fi\n'
                if name == "mint-release-tag.sh"
                else (
                    'if [[ "${FAKE_RELEASE_RUN_FAIL:-}" == "true" ]]; then exit 6; fi\n'
                    if name == "wait-tag-release-run.sh"
                    else (
                        'if [[ "${FAKE_CONSUMER_FAIL:-}" == "true" ]]; then exit 5; fi\n'
                        if name == "consumer-smoke.sh"
                        else (
                            'if [[ "${FAKE_DISPATCH_FAIL:-}" == "true" ]]; then exit 4; fi\n'
                            if name == "dispatch-downstreams.sh"
                            else ""
                        )
                    )
                )
            )
            output = (
                'printf \'%s\\n\' \'{"run_id":123,"attempt":1,"url":"https://example.test/run"}\'\n'
                if name == "wait-tag-release-run.sh"
                else f'printf "%s\\n" "{name}" >> "$FAKE_MUTATION_LOG"\n'
            )
            self.executable(
                self.helper / "scripts" / name,
                "#!/usr/bin/env bash\n"
                + failure
                + output,
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def executable(path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def run_recovery(
        self,
        *,
        registry_state: str,
        body: str = MARKER,
        mint_fails: bool = False,
        tag_present: bool = True,
        release_run_fails: bool = False,
        consumer_fails: bool = False,
        dispatch_fails: bool = False,
        initial_release_absent: bool = False,
        publication_recovers: bool = True,
        release_edit_fail_at: int = 0,
        deadline: int = 3700,
    ) -> subprocess.CompletedProcess:
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{environment['PATH']}",
                "PACKAGE": "kin-demo",
                "MANIFEST": "Cargo.toml",
                "KIN_ACTIONS_HELPER": str(self.helper),
                "KIN_READ_TOKEN": "read",
                "KIN_ACTIONS_TOKEN": "actions",
                "KIN_RELEASE_TAG_TOKEN": "release",
                "KIN_DOWNSTREAM_DISPATCH_TOKEN": "dispatch",
                "KIN_RECOVERY_SUMMARY": str(self.summary),
                "KIN_RECOVERY_DEADLINE_EPOCH": str(deadline),
                "GITHUB_OUTPUT": str(self.output),
                "GITHUB_REPOSITORY": "kinlab-ai/kin-demo",
                "GITHUB_EVENT_REPOSITORY_DEFAULT_BRANCH": "main",
                "FAKE_REGISTRY_STATE": registry_state,
                "FAKE_REGISTRY_STATE_FILE": str(
                    self.root / "registry-state"
                ),
                "FAKE_PUBLICATION_RECOVERY_FAIL": (
                    "false" if publication_recovers else "true"
                ),
                "FAKE_RELEASE_JSON": json.dumps(
                    {
                        "isDraft": False,
                        "isPrerelease": False,
                        "body": body,
                    }
                ),
                "FAKE_MUTATION_LOG": str(self.log),
                "FAKE_RELEASE_VIEWS": str(self.release_views),
                "FAKE_RELEASE_EDITS": str(self.release_edits),
                "FAKE_RELEASE_EDIT_FAIL_AT": str(release_edit_fail_at),
                "FAKE_MINT_FAIL": "true" if mint_fails else "false",
                "FAKE_CONSUMER_FAIL": (
                    "true" if consumer_fails else "false"
                ),
                "FAKE_DISPATCH_FAIL": (
                    "true" if dispatch_fails else "false"
                ),
                "FAKE_TAG_PRESENT": "true" if tag_present else "false",
                "FAKE_RELEASE_RUN_FAIL": (
                    "true" if release_run_fails else "false"
                ),
                "FAKE_RELEASE_INITIAL_ABSENT": (
                    "true" if initial_release_absent else "false"
                ),
                "FAKE_TIMEOUT_LOG": str(self.timeout_log),
                "FAKE_NOW_EPOCH": "1000",
            }
        )
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def outputs(self) -> dict[str, str]:
        return dict(
            line.split("=", 1)
            for line in self.output.read_text(encoding="utf-8").splitlines()
        )

    def output_values(self, name: str) -> list[str]:
        prefix = f"{name}="
        return [
            line.removeprefix(prefix)
            for line in self.output.read_text(encoding="utf-8").splitlines()
            if line.startswith(prefix)
        ]

    def mutations(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_absent_registry_row_is_recovered_before_post_publish_action(
        self,
    ) -> None:
        result = self.run_recovery(registry_state="version-absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs()["recovery_state"], "complete")
        self.assertEqual(self.mutations(), [])

    def test_unrecovered_registry_row_fails_before_post_publish_action(
        self,
    ) -> None:
        result = self.run_recovery(
            registry_state="version-absent",
            publication_recovers=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.outputs()["recovery_state"], "awaiting-publication"
        )
        self.assertEqual(self.mutations(), [])

    def test_existing_delivery_marker_makes_recovery_idempotent(self) -> None:
        result = self.run_recovery(registry_state="available")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs()["recovery_state"], "complete")
        self.assertEqual(
            self.mutations(),
            [],
        )

    def test_missing_marker_dispatches_then_persists_marker(self) -> None:
        result = self.run_recovery(registry_state="available", body="notes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.mutations(),
            [
                "consumer-smoke.sh",
                "release-edit",
                "dispatch-downstreams.sh",
                "release-edit",
            ],
        )
        self.assertEqual(self.outputs()["recovery_state"], "complete")
        self.assertEqual(
            self.output_values("recovery_state"),
            [
                "inspecting",
                "registry-available",
                "consumer-proven",
                "tag-present",
                "release-finalized",
                "downstreams-dispatched",
                "complete",
            ],
        )

    def test_tag_failure_stops_before_release_or_downstreams(self) -> None:
        result = self.run_recovery(
            registry_state="available",
            body="notes",
            mint_fails=True,
            tag_present=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.mutations(),
            ["consumer-smoke.sh"],
        )
        self.assertEqual(self.outputs()["recovery_state"], "consumer-proven")
        self.assertEqual(self.outputs()["failed_phase"], "mint-tag")

    def test_failed_tag_release_gate_cannot_finalize_or_dispatch(self) -> None:
        result = self.run_recovery(
            registry_state="available",
            body="notes",
            release_run_fails=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(), ["consumer-smoke.sh"])
        self.assertEqual(self.outputs()["recovery_state"], "tag-present")
        self.assertEqual(self.outputs()["failed_phase"], "wait-tag-release")
        self.assertNotIn("release create", result.stdout + result.stderr)

    def test_release_view_race_is_refreshed_only_after_gate_success(self) -> None:
        result = self.run_recovery(
            registry_state="available",
            body="notes",
            initial_release_absent=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(
            int(self.release_views.read_text(encoding="utf-8")),
            2,
        )
        self.assertNotIn("release create", result.stdout + result.stderr)

    def test_failure_after_each_durable_boundary_reports_last_proven_state(
        self,
    ) -> None:
        cases = (
            (
                "consumer",
                {"consumer_fails": True},
                "registry-available",
                "consumer-smoke",
            ),
            (
                "tag",
                {"tag_present": False, "mint_fails": True},
                "consumer-proven",
                "mint-tag",
            ),
            (
                "release",
                {"release_run_fails": True},
                "tag-present",
                "wait-tag-release",
            ),
            (
                "downstream",
                {"dispatch_fails": True},
                "release-finalized",
                "dispatch-downstreams",
            ),
            (
                "downstream marker",
                {"release_edit_fail_at": 2},
                "release-finalized",
                "mark-downstreams",
            ),
        )
        for label, kwargs, state, phase in cases:
            with self.subTest(label=label):
                self.output.unlink(missing_ok=True)
                self.summary.unlink(missing_ok=True)
                self.log.unlink(missing_ok=True)
                self.release_views.unlink(missing_ok=True)
                self.release_edits.unlink(missing_ok=True)
                result = self.run_recovery(
                    registry_state="available",
                    body="notes",
                    **kwargs,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.outputs()["recovery_state"], state)
                self.assertEqual(self.outputs()["failed_phase"], phase)

    def test_publication_failure_reports_awaiting_boundary(self) -> None:
        result = self.run_recovery(
            registry_state="version-absent",
            publication_recovers=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.output_values("recovery_state"),
            ["inspecting", "awaiting-publication"],
        )
        self.assertEqual(
            self.outputs()["failed_phase"],
            "recover-publication",
        )

    def test_aggregate_deadline_caps_every_external_timeout(self) -> None:
        result = self.run_recovery(
            registry_state="available",
            deadline=1050,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        timeouts = [
            int(line)
            for line in self.timeout_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(timeouts)
        self.assertTrue(all(0 < value <= 50 for value in timeouts), timeouts)

    def test_expired_aggregate_deadline_fails_before_external_mutation(
        self,
    ) -> None:
        result = self.run_recovery(
            registry_state="available",
            deadline=1000,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.outputs()["recovery_state"], "inspecting")
        self.assertEqual(self.outputs()["failed_phase"], "inspect-authority")
        self.assertEqual(self.mutations(), [])


if __name__ == "__main__":
    unittest.main()
