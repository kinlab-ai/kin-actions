"""Static security and authority contracts for full-auto release workflows."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def token_block(text: str, step_id: str) -> str:
    match = re.search(
        rf"(?ms)^      - name: .*?\n"
        rf"(?:        .*\n)*?"
        rf"        id: {re.escape(step_id)}\n"
        rf".*?(?=^      - |\Z)",
        text,
    )
    if not match:
        raise AssertionError(f"token step not found: {step_id}")
    return match.group(0)


class FullAutoWorkflowContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cargo = read(".github/workflows/cargo-release-train.yml")
        cls.recovery = read(".github/workflows/cargo-release-recovery.yml")
        cls.dependency = read(".github/workflows/cargo-dependency-wave.yml")
        cls.self_train = read(".github/workflows/self-release-train.yml")
        cls.release = read(".github/workflows/release.yml")
        cls.pin = read(".github/workflows/pin-wave.yml")
        cls.reconcile = read("scripts/reconcile-release-pr.sh")
        cls.finalize = read("scripts/finalize-release-pr.sh")
        cls.pin_reconcile = read("scripts/reconcile-workflow-pin-pr.sh")
        cls.pin_wave = read("scripts/run-workflow-pin-wave.sh")

    def test_controllers_are_default_branch_event_surfaces(self) -> None:
        for name, text in (
            ("self train", self.self_train),
            ("release", self.release),
            ("pin", self.pin),
        ):
            with self.subTest(name=name):
                self.assertNotIn("workflow_dispatch:", text)
                self.assertIn("schedule:", text)
        self.assertIn("workflow_run:", self.self_train)
        self.assertIn("workflow_run:", self.release)
        self.assertIn("repository_dispatch:", self.pin)

    def test_recovery_alarm_lands_on_kin_infra_with_an_issues_only_token(self) -> None:
        # The terminal issue moved off the calling repository's public tab onto
        # kinlab-ai/kin-infra. The caller-scoped token cannot write there, so
        # the alarm mints its own App token scoped to the caller and kin-infra
        # with issues: write and nothing else, and every issue call in the
        # terminal step targets kin-infra through that token.
        # Steps are sliced by their name lines rather than matched with a
        # multi-line regex: the id-anchored pattern above the class backtracks
        # across the whole file for a step this far down and never returns.
        def step_block(text: str, name: str) -> str:
            start = text.index(f"      - name: {name}\n")
            end = text.find("\n      - name: ", start + 1)
            return text[start:] if end == -1 else text[start:end]

        mint = step_block(
            self.recovery,
            "Mint the alarm token, issues only, this repository and kin-infra",
        )
        self.assertIn("id: alarm_token", mint)
        self.assertIn("if: always()", mint)
        self.assertIn("permission-issues: write", mint)
        self.assertIn("kin-infra", mint)
        self.assertIn("${{ github.event.repository.name }}", mint)
        for broader in ("permission-contents", "permission-pull-requests", "permission-actions"):
            self.assertNotIn(broader, mint)

        step = step_block(self.recovery, "Reconcile terminal failure issue")
        self.assertIn("Fail visibly", self.recovery[self.recovery.index(step) + len(step):])
        self.assertIn("GH_TOKEN: ${{ steps.alarm_token.outputs.token }}", step)
        self.assertIn("ALARM_REPO: kinlab-ai/kin-infra", step)
        self.assertIn("ALARM_LABEL: ${{ github.event.repository.name }}", step)
        self.assertIn('--label "$ALARM_LABEL"', step)
        self.assertNotIn('--repo "$GITHUB_REPOSITORY"', step)
        self.assertNotIn("GH_TOKEN: ${{ github.token }}", step)

        dry_run = read(".github/workflows/alarm-dry-run.yml")
        self.assertNotIn("workflow_dispatch:", dry_run)
        self.assertIn("repository_dispatch:", dry_run)
        self.assertIn("environment: release-tag", dry_run)
        self.assertIn("permission-issues: write", dry_run)
        self.assertIn("ALARM_REPO: kinlab-ai/kin-infra", dry_run)

    def test_every_train_is_serialized_without_cancellation(self) -> None:
        for text in (
            self.cargo,
            self.recovery,
            self.self_train,
            self.release,
            self.pin,
        ):
            self.assertIn("concurrency:", text)
            self.assertIn("cancel-in-progress: false", text)

    def test_pending_run_replacement_cannot_drop_release_intent(self) -> None:
        # GitHub keeps only one pending member of a concurrency group. Both
        # release trains must therefore reconstruct intent from durable state,
        # never from whichever notification happened to survive.
        for name, text in (
            ("cargo", self.cargo),
            ("self", self.self_train),
        ):
            with self.subTest(name=name):
                self.assertIn(
                    "resolve-release-intent.py",
                    text,
                )
                self.assertIn('--base-ref "$BASE_REF"', text)
                self.assertNotIn("/commits/${sha}/pulls", text)
                self.assertNotIn("gh pr list", text)
                self.assertNotIn(
                    "github.event.client_payload.release_intent",
                    text,
                )

        # Publication and pin delivery likewise derive from durable current
        # state, so a later survivor subsumes any replaced pending event.
        self.assertIn(
            "version=\"$(tr -d '[:space:]' < VERSION)\"",
            self.release,
        )
        self.assertIn(
            'gh api "repos/${GITHUB_REPOSITORY}/releases/latest"',
            self.pin,
        )
        self.assertIn('print(*sorted(data["repositories"])', self.pin)
        self.assertIn("print(latest)", self.pin)

    def test_pin_wave_proves_release_lineage_before_pinning(self) -> None:
        # A finalized tag carrying the right VERSION is not enough: the fleet
        # may only be pinned to a commit on the default-branch lineage.
        self.assertIn('git rev-list --first-parent HEAD', self.pin)
        self.assertIn('grep -cx "$tag_sha"', self.pin)
        self.assertIn('if [[ "$lineage" != "1" ]]; then', self.pin)
        self.assertNotIn('grep -q "$tag_sha"', self.pin)

    def test_pin_wave_runs_only_the_exact_finalized_release_controller(self) -> None:
        self.assertIn("Checkout exact finalized rollout controller", self.pin)
        self.assertIn("ref: ${{ steps.release.outputs.tag_sha }}", self.pin)
        self.assertIn("path: .kin-actions-release", self.pin)
        self.assertIn(
            "KIN_ACTIONS_ROOT: ${{ github.workspace }}/.kin-actions-release",
            self.pin,
        )
        self.assertIn(
            "${{ github.workspace }}/.kin-actions-release/.kin-release/consumers.json",
            self.pin,
        )
        self.assertIn('protocol_required="$(' , self.pin)
        self.assertIn(">= (0, 1, 34)", self.pin)
        self.assertIn("v${TARGET_VERSION} must carry the exact schema-2", self.pin)

    def test_general_release_tokens_cannot_edit_workflows(self) -> None:
        for name, text in (
            ("cargo", self.cargo),
            ("recovery", self.recovery),
            ("dependency", self.dependency),
            ("self", self.self_train),
            ("release", self.release),
        ):
            with self.subTest(name=name):
                self.assertIn("permission-contents: write", text)
                self.assertNotIn("permission-workflows:", text)
        self.assertIn("permission-workflows: write", self.pin)

    def test_all_external_actions_are_immutable(self) -> None:
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            for action, reference in re.findall(
                r"(?m)^\s*(?:-\s*)?uses:\s*([^@\s]+)@([^\s#]+)",
                text,
            ):
                if action.startswith("./"):
                    continue
                with self.subTest(workflow=path.name, action=action):
                    self.assertRegex(reference, r"^[0-9a-f]{40}$")

    def test_pin_controller_uses_separate_environment_and_secret_contract(self) -> None:
        self.assertIn("environment: release-followups", self.pin)
        self.assertIn("KIN_WORKFLOW_PIN_APP_ID", self.pin)
        self.assertIn("KIN_WORKFLOW_PIN_APP_PRIVATE_KEY", self.pin)
        self.assertNotIn("KIN_RELEASE_BOT_APP_ID", self.pin)
        self.assertIn(".kin-release/consumers.json", self.pin)
        self.assertIn("installation/repositories", self.pin)
        self.assertIn("permission-actions: read", self.pin)
        self.assertIn("permission-checks: read", self.pin)
        self.assertIn("permission-administration: read", self.pin)
        self.assertNotIn("permission-administration: write", self.pin)
        self.assertIn("Hold for external no-bypass tag-freeze audit", self.pin)
        self.assertNotIn("KIN_RULESET_AUDIT_TOKEN", self.pin)
        self.assertIn("run-workflow-pin-wave.sh", self.pin)
        self.assertIn("workflow-pin-rollout.py", self.pin_wave)
        self.assertIn("--checkouts-root", self.pin_wave)

    def test_pin_updater_requires_an_exact_live_manifest(self) -> None:
        updater = read("scripts/update-kin-actions-pins.py")
        self.assertIn("def discover_pin_paths(", updater)
        self.assertIn("unmanifested live pins", updater)
        self.assertIn("manifest paths without live pins", updater)

    def test_general_train_crosses_main_only_through_server_merge(self) -> None:
        neutralize = self.reconcile.index(" neutralize ")
        post_merge = self.reconcile.index(
            'gh api --method POST "repos/${GITHUB_REPOSITORY}/merges"'
        )
        validate = self.reconcile.index(" validate-merge ")
        self.assertLess(neutralize, post_merge)
        self.assertLess(post_merge, validate)
        self.assertNotRegex(
            self.reconcile,
            r"(?m)git\s+push[^\n]*(?:--force|-f\b)",
        )
        self.assertNotIn("--method DELETE", self.reconcile)
        self.assertNotIn("refs/heads/${TRAIN_BRANCH}\" -f force=true", self.reconcile)
        self.assertIn('jq -r .changed <<<"$neutralization"', self.reconcile)
        self.assertIn("git commit --allow-empty -s", self.reconcile)

    def test_pin_train_never_deletes_or_force_rewrites(self) -> None:
        self.assertIn(
            'gh api --method POST "repos/${TARGET_REPOSITORY}/merges"',
            self.pin_reconcile,
        )
        self.assertNotRegex(
            self.pin_reconcile,
            r"(?m)git\s+push[^\n]*(?:--force|-f\b)",
        )
        self.assertNotIn("--method DELETE", self.pin_reconcile)
        self.assertNotIn("delete-branch", self.pin_reconcile)
        self.assertIn(
            'jq -r .changed <<<"$neutralization"', self.pin_reconcile
        )
        self.assertIn("git commit --allow-empty -s", self.pin_reconcile)
        self.assertIn(
            'git rev-parse "${head}^{tree}"', self.pin_reconcile
        )

    def test_pin_pr_head_is_resolved_rather_than_read_once(self) -> None:
        """A pull request object trails its ref, so one read is not an answer.

        The wave read ``headRefOid`` a second after pushing and refused its own
        commit whenever GitHub had not caught up. It is a race, not a
        certainty: refusals clustered about a second after the push and the
        pushes that survived were read several seconds later. Keep the
        exact-head decision on the resolver, which re-reads until the pushed
        commit appears.
        """

        resolve = read("scripts/resolve-workflow-pin-pr.py")
        self.assertIn("resolve-workflow-pin-pr.py", self.pin_reconcile)
        self.assertIn('--expect-head "$head"', self.pin_reconcile)
        self.assertNotIn("headRefOid", self.pin_reconcile)
        self.assertIn("def resolve_pin_pr(", resolve)
        self.assertIn("for attempt in range(attempts):", resolve)
        # Ambiguity and foreign ownership must stay immediate refusals. Waiting
        # on either would turn a decision into a timeout.
        self.assertIn("multiple workflow-pin PRs claim", resolve)
        self.assertIn("head repository owner is", resolve)

    def test_auto_merge_is_bound_to_exact_generated_head(self) -> None:
        for script in (self.finalize, self.pin_wave):
            self.assertIn("--match-head-commit", script)
            self.assertIn("--auto --squash", script)
        self.assertNotIn("--auto --squash", self.pin_reconcile)
        self.assertIn("--disable-auto", self.pin_reconcile)
        self.assertLess(
            self.pin_wave.index("check-workflow-pin-pr.py"),
            self.pin_wave.index('gh pr merge "$pr"'),
        )
        self.assertIn(
            '--match-head-commit "$PR_HEAD"',
            self.dependency,
        )
        self.assertIn(
            "steps.open_pr.outputs.pull-request-head-sha",
            self.dependency,
        )
        self.assertIn(
            "validate-dependency-wave.py",
            self.dependency,
        )
        self.assertIn(
            '"${KIN_ACTIONS_SHA}:scripts/validate-dependency-wave.py"',
            self.dependency,
        )
        self.assertIn(
            "add-paths: ${{ steps.admission.outputs.add_paths }}",
            self.dependency,
        )
        self.assertIn(
            'if [[ "$api_tree" != "$EXPECTED_TREE" ]]',
            self.dependency,
        )
        self.assertIn(
            "--expected-tree \"$EXPECTED_TREE\"",
            self.dependency,
        )

    def test_called_cargo_train_helpers_use_exact_workflow_identity(self) -> None:
        self.assertIn(
            "repository: ${{ job.workflow_repository }}", self.cargo
        )
        self.assertIn("ref: ${{ job.workflow_sha }}", self.cargo)

    def test_cargo_train_feeds_the_exact_generated_allowlist_to_policy(self) -> None:
        self.assertIn(
            "ALLOWED_PATHS_JSON: ${{ steps.state.outputs.allowed_paths_json }}",
            self.cargo,
        )
        self.assertIn("jq -r '.[]' <<<\"$ALLOWED_PATHS_JSON\"", self.cargo)
        self.assertIn(
            'generated_args+=(--generated-path "$path")',
            self.cargo,
        )
        self.assertIn('"${generated_args[@]}"', self.cargo)

    def test_release_controller_reconciles_tag_release_then_pin_event(self) -> None:
        mint = self.release.index("scripts/mint-release-tag.sh")
        github_release = self.release.index('gh release create "$TAG"')
        dispatch = self.release.index('"kin-actions-pin-reconcile"')
        self.assertLess(mint, github_release)
        self.assertLess(github_release, dispatch)
        self.assertIn("resolve-version-commit.py", self.release)
        self.assertIn(
            "GITHUB_SHA: ${{ steps.state.outputs.version_commit }}",
            self.release,
        )
        self.assertIn(
            'if [[ "$tag_sha" != "$version_commit" ]]',
            self.release,
        )

    def test_train_tags_are_bound_to_their_version_authority(self) -> None:
        self.assertIn("--inspect-ref \"$tag_sha\"", self.cargo)
        self.assertIn('git show "${tag_sha}:VERSION"', self.self_train)

    def test_cargo_tag_delivery_cannot_republish(self) -> None:
        registry = read(".github/workflows/cargo-registry-release.yml")
        publish = re.search(
            r"(?ms)^  publish:\n(.*?)(?=^  [a-zA-Z0-9_]+:\n|\Z)",
            registry,
        )
        self.assertIsNotNone(publish)
        block = publish.group(1)
        self.assertIn("github.ref == 'refs/heads/main'", block)
        self.assertNotIn("refs/tags", block)

    def test_cargo_recovery_is_bounded_idempotent_and_never_publishes(self) -> None:
        script = read("scripts/recover-cargo-release.sh")
        self.assertIn("timeout-minutes: 55", self.recovery)
        self.assertIn('CONTROLLER_WINDOW_SECONDS: "2700"', self.recovery)
        self.assertIn('TERMINAL_RESERVE_SECONDS: "600"', self.recovery)
        self.assertIn('OUTER_JOB_SECONDS: "3300"', self.recovery)
        self.assertIn(
            "CONTROLLER_WINDOW_SECONDS + TERMINAL_RESERVE_SECONDS > "
            "OUTER_JOB_SECONDS",
            self.recovery,
        )
        self.assertIn("KIN_RECOVERY_DEADLINE_EPOCH:", self.recovery)
        self.assertIn("continue-on-error: true", self.recovery)
        self.assertIn("steps.recovery.outcome != 'success'", self.recovery)
        self.assertIn(
            "steps.recovery.outputs.recovery_state != 'complete'",
            self.recovery,
        )
        self.assertIn("permission-issues: write", self.recovery)
        self.assertIn("[release-recovery]", self.recovery)
        self.assertIn("inspect-registry-version.py", script)
        self.assertIn("recover-registry-publish.py", script)
        self.assertIn("REGISTRY_WORKFLOW", script)
        self.assertIn("resolve-version-commit.py", script)
        self.assertIn("consumer-smoke.sh", script)
        self.assertIn("mint-release-tag.sh", script)
        self.assertIn("wait-tag-release-run.sh", script)
        self.assertIn("KIN_ACTIONS_TOKEN", script)
        self.assertIn(
            "<!-- kin-cargo-release:downstreams-dispatched -->",
            script,
        )
        self.assertIn(
            "<!-- kin-cargo-release:consumer-smoke-passed -->",
            script,
        )
        self.assertIn("version-absent", script)
        self.assertIn("awaiting-publication", script)
        for state in (
            "registry-available",
            "consumer-proven",
            "tag-present",
            "release-finalized",
            "downstreams-dispatched",
            "complete",
        ):
            self.assertIn(f"emit_state {state}", script)
        self.assertIn("emit failed_phase", script)
        self.assertIn("budget_for()", script)
        self.assertIn("timeout --signal=TERM --kill-after=5s", script)
        self.assertIn('hard_timeout "$publication_seconds"', script)
        self.assertIn('hard_timeout "$smoke_seconds"', script)
        self.assertIn('hard_timeout "$release_seconds"', script)
        self.assertIn('hard_timeout "$downstream_seconds"', script)
        self.assertNotRegex(
            script,
            r"(?m)\bcargo\s+publish\b|\bpublish-crate\.sh\b",
        )
        self.assertNotIn("gh release create", script)
        self.assertNotRegex(
            script,
            r"(?m)git\s+(?:push\s+.*(?:--force|-f\b)|tag\s+-f\b)",
        )

    def test_generated_pr_checks_have_bounded_exact_head_recovery(self) -> None:
        self.assertIn("actions: write", self.cargo)
        self.assertIn("checks: read", self.cargo)
        self.assertIn("issues: write", self.cargo)
        self.assertIn("pull-requests: read", self.cargo)
        self.assertIn("recover-release-pr-checks.py", self.cargo)
        self.assertIn(
            '--expected-head "${{ steps.finalize.outputs.train_head }}"',
            self.cargo,
        )
        self.assertIn("--actions-app-id 15368", self.cargo)
        self.assertNotIn("--expected-workflow", self.cargo)
        self.assertIn(
            '"required_check_recovery": '
            '"branch-required+github-actions-app-15368+exact-head+'
            'same-repo+pull-request"',
            read(".kin-release/cargo-train-bootstrap.json"),
        )
        self.assertIn("[release-train]", self.cargo)
        self.assertIn("--disable-auto", self.cargo)
        self.assertIn('disarm_state="head-moved-no-mutation"', self.cargo)
        self.assertIn('echo "- auto-merge disarm:', self.cargo)
        self.assertIn(
            "steps.check_recovery.outcome != 'success'",
            self.cargo,
        )
        self.assertIn(
            "steps.check_recovery.outputs.check_recovery_state != 'merged'",
            self.cargo,
        )
        self.assertIn("KIN_PROTECTION_TOKEN:", self.cargo)

    def test_terminal_escalation_survives_release_app_failure(self) -> None:
        for workflow in (self.cargo, self.recovery):
            self.assertIn("issues: write", workflow)
            self.assertIn("if: always()", workflow)
        # The train's terminal issue still files with the caller-scoped token.
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.cargo)
        # The recovery's terminal issue moved to kin-infra, which the
        # caller-scoped token cannot reach, so it files through the alarm mint:
        # its own `if: always()` mint that never reads the recovery App mint's
        # outcome or token, so a recovery mint that failed still gets its
        # terminal issue. A failed alarm mint is that step's own loud failure,
        # never a fallback to the workflow token.
        recovery_terminal = self.recovery.split(
            "- name: Reconcile terminal failure issue", 1
        )[1].split("- name: Fail visibly", 1)[0]
        self.assertIn(
            "GH_TOKEN: ${{ steps.alarm_token.outputs.token }}", recovery_terminal
        )
        self.assertNotIn("steps.release_app.outputs.token", recovery_terminal)
        self.assertNotIn("GH_TOKEN: ${{ github.token }}", recovery_terminal)
        alarm_mint = self.recovery.split(
            "- name: Mint the alarm token, issues only, this repository and kin-infra", 1
        )[1].split("- name: Reconcile terminal failure issue", 1)[0]
        self.assertIn("if: always()", alarm_mint)
        self.assertNotIn("steps.release_app", alarm_mint)
        self.assertIn(
            "KIN_RELEASE_MUTATION_TOKEN: ${{ steps.release_app.outputs.token }}",
            self.cargo,
        )
        self.assertIn('disarm_state="release-app-unavailable"', self.cargo)
        terminal = self.cargo.split(
            "- name: Reconcile generated release PR terminal issue", 1
        )[1].split("- name: Fail visibly", 1)[0]
        self.assertNotIn(
            "working-directory: caller",
            terminal,
        )
        self.assertIn(
            "CALLER_CHECKOUT_OUTCOME: "
            "${{ steps.caller_checkout.outcome }}",
            terminal,
        )
        self.assertIn(
            "HELPER_CHECKOUT_OUTCOME: "
            "${{ steps.helper_checkout.outcome }}",
            terminal,
        )
        self.assertIn('health_state="stale-trigger-no-op"', self.cargo)
        self.assertIn('health_state="release-recovery-owned-no-op"', self.cargo)
        self.assertIn('health_state="no-release-needed"', self.cargo)
        self.assertIn(
            'gh issue close "$issue" --repo "$GITHUB_REPOSITORY"',
            terminal,
        )
        self.assertIn(
            '--comment "Automatic release controller is healthy '
            '(${health_state})."',
            terminal,
        )
        self.assertIn(
            'if [[ "$JOB_STATUS" == "success" &&',
            self.recovery,
        )
        # The close lands on the alarm repository like the create does.
        self.assertIn(
            'gh issue close "$issue" --repo "$ALARM_REPO"',
            self.recovery,
        )
        self.assertIn("- failed phase:", self.recovery)

    def test_caller_release_workflow_pins_are_live_admission(self) -> None:
        self.assertIn(
            "check-release-workflow-pins.py",
            self.cargo,
        )
        self.assertIn(
            '--workflow "caller/.github/workflows/release.yml"',
            self.cargo,
        )
        bootstrap = read(".kin-release/cargo-train-bootstrap.json")
        self.assertIn('"external_uses_ref": "40-lowercase-hex-sha"', bootstrap)
        self.assertIn('"app_id": 15368', bootstrap)

    def test_train_dependency_wave_requires_app_and_never_bumps_own_version(self) -> None:
        self.assertIn("version-mode:", self.dependency)
        self.assertIn("release-environment:", self.dependency)
        self.assertIn(
            "train-mode dependency waves require bump-own-version=false",
            self.dependency,
        )
        self.assertIn(
            'if [[ "$GITHUB_REF" != "refs/heads/${DEFAULT_BRANCH}" ]]',
            self.dependency,
        )
        self.assertIn("KIN_RELEASE_BOT_APP_ID", self.dependency)
        self.assertIn("KIN_RELEASE_BOT_PRIVATE_KEY", self.dependency)
        for permission in (
            "permission-contents: write",
            "permission-pull-requests: write",
            "permission-issues: write",
        ):
            self.assertIn(permission, self.dependency)
        self.assertIn("steps.release_app.outputs.token", self.dependency)
        self.assertIn(
            "train mode refuses PAT and GITHUB_TOKEN fallback",
            self.dependency,
        )
        self.assertNotIn("permission-workflows:", self.dependency)

    def test_manifest_contains_every_live_external_consumer_path(self) -> None:
        manifest = json.loads(read(".kin-release/consumers.json"))
        expected = {
            "kinlab-ai/kin",
            "kinlab-ai/kin-bench",
            "kinlab-ai/kinlab",
            "kinlab-ai/kin-model",
            "kinlab-ai/kin-db",
            "kinlab-ai/kin-lsp",
            "kinlab-ai/kin-vfs",
            "kinlab-ai/kin-infer",
            "kinlab-ai/kin-vector",
            "kinlab-ai/kin-search",
            "kinlab-ai/kin-blobs",
            "kinlab-ai/kin-bench-spec",
            "kinlab-ai/kin-editor",
            "kinlab-ai/kin-infra",
            "kinlab-ai/homebrew-kin",
        }
        self.assertEqual(manifest["schema"], 2)
        self.assertEqual(set(manifest["repositories"]), expected)
        self.assertEqual(
            sum(
                len(spec["workflow_paths"])
                for spec in manifest["repositories"].values()
            ),
            35,
        )

    def test_activation_contract_limits_ruleset_bypass_to_bot_branches(self) -> None:
        readme = " ".join(read("README.md").split())
        self.assertIn("no Workflows permission or `main` bypass", readme)
        self.assertIn(
            "the exact `automation/release-next` branch bypass",
            readme,
        )
        self.assertIn(
            "`automation/kin-registry-dependency-wave`",
            readme,
        )
        self.assertIn(
            "only the exact `automation/kin-actions-pin-next` branch bypass",
            readme,
        )
        self.assertIn(
            "a second freeze ruleset blocks tag update, deletion, and "
            "non-fast-forward without any release-App bypass",
            readme,
        )

    def test_activation_contract_names_live_settings_and_checks(self) -> None:
        required = (
            "release / Version bump gate",
            "release / Registry-only build",
            "release / Repo verification",
        )
        for context in required:
            self.assertIn(context, self.cargo)
            self.assertIn(context, self.dependency)
            self.assertIn(context, read(".kin-release/cargo-train-bootstrap.json"))
            self.assertIn(context, read("README.md"))
        for workflow in (self.cargo, self.dependency, self.self_train):
            self.assertIn("check-release-activation.py", workflow)
        for workflow in (self.cargo, self.dependency):
            self.assertIn("--required-check-app-id 15368", workflow)
        for workflow in (self.cargo, self.dependency):
            self.assertIn(
                "branches/${DEFAULT_BRANCH}/protection/required_status_checks",
                workflow,
            )
            self.assertIn(
                "permission-administration: read",
                workflow,
            )
        for setting in (
            "allow_auto_merge",
            "allow_squash_merge",
            "allow_merge_commit",
            "allow_rebase_merge",
            "squash_merge_commit_title",
            "squash_merge_commit_message",
        ):
            self.assertIn(setting, read("README.md"))
            self.assertIn(
                setting,
                read(".kin-release/cargo-train-bootstrap.json"),
            )
        self.assertIn("PR_BODY", read("CONTRIBUTING.md"))

    def test_every_authoritative_surface_uses_immutable_intent(self) -> None:
        agents = read("AGENTS.md")
        readme = read("README.md")
        contributing = read("CONTRIBUTING.md")
        for text in (agents, readme, contributing):
            self.assertIn("Kin-Release-Intent", text)
        self.assertIn("zero automatic release-intent authority", agents)
        self.assertIn(
            "Mutable PR labels have zero release-intent authority",
            " ".join(readme.split()),
        )
        self.assertIn("Mutable PR", contributing)
        for workflow in (self.cargo, self.self_train):
            self.assertIn("resolve-release-intent.py", workflow)

    def test_pin_branch_default_matches_the_version_gate_exemption(self) -> None:
        """The two halves of the pin-chore exemption must name one branch.

        The wave picks the branch in `reconcile-workflow-pin-pr.sh`; the gate
        exempts it by `--pin-branch` default in `check-version-bump.py`. The
        gate runs inside the consumer repo and cannot observe the wave's env,
        so a drift on either side silently returns every pin PR to the
        unmergeable state the exemption exists to prevent.
        """

        gate = read("scripts/check-version-bump.py")
        shell_default = re.search(
            r'^PIN_BRANCH="\$\{PIN_BRANCH:-([^}"]+)\}"',
            self.pin_reconcile,
            re.M,
        )
        self.assertIsNotNone(
            shell_default, "reconcile-workflow-pin-pr.sh must default PIN_BRANCH"
        )
        gate_default = re.search(
            r'"--pin-branch",\s*\n\s*default="([^"]+)"', gate
        )
        self.assertIsNotNone(
            gate_default, "check-version-bump.py must default --pin-branch"
        )
        self.assertEqual(shell_default.group(1), gate_default.group(1))


if __name__ == "__main__":
    unittest.main()
