#!/usr/bin/env python3
"""Falsifiers for the live workflow-run lookup guard.

Run with: ``python3 -m unittest discover -s scripts -p 'test_*.py'``

A guard that has never been shown to fail proves nothing, and a guard shown to
fail once proves only that it fails there. Every case here builds a throwaway
Git checkout, stages fixture files, and runs the real checker end to end, so
the tracked-file enumeration, the allowlist contract, and the exit-code
classification are all exercised rather than described.

The three failure classes stay provably distinct: a found lookup exits 1, a
broken allowlist exits 3, and a guard that cannot run exits 4. The most
important case in this file is the one where both of the first two happen at
once, because the failure mode worth preventing is an allowlist mistake that
quietly suppresses the scan and reports a clean tree.

This file quotes the very patterns the guard rejects, which is why the guard
skips test-named paths by convention and prints how many it skipped.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
GUARD = SCRIPTS / "workflow-run-lookup-guard.py"
REPO = SCRIPTS.parent
WORKFLOW = REPO / ".github" / "workflows" / "workflow-run-lookup-guard.yml"
SELF_TEST = REPO / ".github" / "workflows" / "self-test.yml"

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_ALLOWLIST = 3
EXIT_RUN_ERROR = 4

# Every must-not-flag case in one file: a branch-ref compare-and-set, a lookup
# of the currently executing run in both spellings, the repo-wide runs list, and
# a human-facing run URL.
CLEAN_WORKFLOW = """\
name: Verify
on: [push]
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - name: Confirm main has not moved
        run: gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/main" --jq .object.sha
      - name: Stamp this run
        run: gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}" --jq .created_at
      - name: Stamp this run again, expression form
        run: gh api "repos/${{ github.repository }}/actions/runs/${{ github.run_id }}" --jq .status
      - name: Refuse to race a concurrent run
        run: gh api "repos/${GITHUB_REPOSITORY}/actions/runs?per_page=50" --jq length
      - name: Link the log for a human
        run: echo "see ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/48"
"""

RUN_ID_LOOKUP_SH = """\
#!/usr/bin/env bash
set -euo pipefail
release_run_id="$1"
run_json="$(gh api "repos/kinlab-ai/kin/actions/runs/${release_run_id}")"
echo "$run_json"
"""

RUNS_LIST_SH = """\
#!/usr/bin/env bash
set -euo pipefail
runs="$(gh api 'repos/kinlab-ai/kin/actions/workflows/release.yml/runs?event=push&per_page=100')"
echo "$runs"
"""

SUB_RESOURCE_SH = """\
#!/usr/bin/env bash
set -euo pipefail
gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/artifacts?per_page=100"
gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${RECOVERY_RUN_ID}/attempts/2/jobs?per_page=100"
"""

CLIENT_CALL_MJS = """\
export async function stale({ github, context }) {
  const runs = await github.rest.actions.listWorkflowRuns({
    owner: context.repo.owner,
    repo: context.repo.repo,
    workflow_id: "release.yml",
  });
  return runs.data.total_count;
}
"""

COMMENTED_SH = """\
#!/usr/bin/env bash
# The old check called gh api repos/x/y/actions/runs/$ID and never recovered
# once that run was deleted, so it verifies the tag object instead now.
tag="${ref#refs/tags/}"
run="$(gh api "repos/x/y/actions/runs/${ID}")"
"""

FORENSIC_SH = """\
#!/usr/bin/env bash
set -euo pipefail
ORIGIN_RUN_ID=29205793134
origin_run_json="$(gh api "repos/${INFRA_REPO}/actions/runs/${ORIGIN_RUN_ID}")"
echo "$origin_run_json"
"""


def extract_run_block(text):
    """Return the workflow's `run: |` body, dedented, ready to execute.

    The step is tested by running it, not by grepping it. A shell property can
    only be certified by a shell.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "run: |":
            continue
        key_indent = len(line) - len(line.lstrip())
        block_indent = None
        body = []
        for raw in lines[index + 1:]:
            if not raw.strip():
                body.append("")
                continue
            indent = len(raw) - len(raw.lstrip())
            if indent <= key_indent:
                break
            if block_indent is None:
                block_indent = indent
            body.append(raw[block_indent:])
        return "\n".join(body) + "\n"
    raise AssertionError("the reusable workflow has no 'run: |' block")


STUB_CHECKER = (
    "import sys\n"
    "print('stub checker argv: ' + ' '.join(sys.argv[1:]))\n"
    "sys.exit({code})\n"
)


def git(args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


class GuardCase(unittest.TestCase):
    """Builds throwaway checkouts and runs the real checker against them."""

    def repo(self, files, untracked=None):
        root = tempfile.mkdtemp(prefix="run-lookup-guard-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for rel, content in files.items():
            path = Path(root) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        git(["init", "-q", "-b", "main"], root)
        # `-f` so a global ignore file cannot quietly drop a fixture and make a
        # violation test pass for the wrong reason. No commit is needed: the
        # guard reads the index through `git ls-files`.
        git(["add", "-A", "-f"], root)
        for rel, content in (untracked or {}).items():
            path = Path(root) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    def allowlist(self, root, entries, name="allowlist.json"):
        path = Path(root) / name
        path.write_text(json.dumps({"allowlist": entries}, indent=2), encoding="utf-8")
        return name

    def run_guard(self, root, *args, checker=None):
        # Stop Git's repository search at the fixture, so a temporary directory
        # created inside another checkout still reads as outside any repository.
        env = dict(os.environ)
        env["GIT_CEILING_DIRECTORIES"] = str(Path(root).resolve().parent)
        result = subprocess.run(
            [sys.executable, str(checker or GUARD), "--root", ".", *args],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.stderr, "", f"guard wrote to stderr: {result.stderr}")
        return result.returncode, result.stdout

    def assertSummary(self, out, label, value):
        self.assertRegex(out, rf"{label}\s*:\s*{value}")


class CleanTree(GuardCase):
    def test_clean_tree_passes(self):
        root = self.repo({".github/workflows/verify.yml": CLEAN_WORKFLOW})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_OK, out)
        self.assertIn("PASS: no live workflow-run lookups", out)

    def test_current_run_lookups_are_counted_not_flagged(self):
        root = self.repo({".github/workflows/verify.yml": CLEAN_WORKFLOW})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_OK, out)
        self.assertSummary(out, "current-run lookups", "2")

    def test_untracked_file_is_not_scanned(self):
        root = self.repo(
            {".github/workflows/verify.yml": CLEAN_WORKFLOW},
            untracked={"scripts/verify.sh": RUN_ID_LOOKUP_SH},
        )
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_OK, out)

    def test_unscanned_file_type_is_ignored(self):
        root = self.repo({"docs/incident.md": RUN_ID_LOOKUP_SH})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_OK, out)
        self.assertSummary(out, "files scanned", "0")

    def test_non_git_directory_is_a_run_error(self):
        root = tempfile.mkdtemp(prefix="run-lookup-guard-nogit-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_RUN_ERROR, out)
        self.assertIn("guard could not run", out)

    def test_checker_exempts_only_itself_and_says_so(self):
        root = self.repo({".github/workflows/verify.yml": CLEAN_WORKFLOW})
        copied = Path(root) / "scripts" / "workflow-run-lookup-guard.py"
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(GUARD, copied)
        git(["add", "-A", "-f"], root)
        rc, out = self.run_guard(root, checker=copied)
        self.assertEqual(rc, EXIT_OK, out)
        self.assertSummary(out, "skipped as checker", "1")


class FoundLookups(GuardCase):
    def test_run_id_lookup_is_reported_with_file_and_line(self):
        root = self.repo({"scripts/verify.sh": RUN_ID_LOOKUP_SH})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_VIOLATIONS, out)
        self.assertIn("scripts/verify.sh:4: workflow-run lookup by id", out)
        self.assertIn("${release_run_id}", out)
        self.assertIn("exit: 1 (violations found)", out)

    def test_workflow_scoped_runs_list_is_reported(self):
        root = self.repo({"scripts/sync.sh": RUNS_LIST_SH})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_VIOLATIONS, out)
        self.assertIn("scripts/sync.sh:3: workflow-scoped runs-list lookup", out)

    def test_sub_resource_of_a_past_run_is_reported_and_self_is_not(self):
        root = self.repo({"scripts/recover.sh": SUB_RESOURCE_SH})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_VIOLATIONS, out)
        self.assertIn("scripts/recover.sh:4:", out)
        self.assertNotIn("scripts/recover.sh:3:", out)

    def test_api_client_call_is_reported(self):
        root = self.repo({".github/scripts/stale.mjs": CLIENT_CALL_MJS})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_VIOLATIONS, out)
        self.assertIn("listWorkflowRuns", out)

    def test_full_line_comment_is_not_flagged_but_code_still_is(self):
        root = self.repo({"scripts/fixed.sh": COMMENTED_SH})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_VIOLATIONS, out)
        self.assertIn("scripts/fixed.sh:5:", out)
        self.assertNotIn("scripts/fixed.sh:2:", out)

    def test_yaml_workflow_body_is_scanned(self):
        dirty = CLEAN_WORKFLOW + (
            '      - name: Resolve the release run\n'
            '        run: gh api "repos/kinlab-ai/kin/actions/runs/${RELEASE_RUN_ID}"\n'
        )
        root = self.repo({".github/workflows/verify.yml": dirty})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_VIOLATIONS, out)
        self.assertIn(".github/workflows/verify.yml:18:", out)

    def test_actions_expression_spelling_is_still_matched(self):
        # An Actions expression carries spaces inside its braces. A pattern that
        # stops at the first space reads this line as no lookup at all, which is
        # the same lookup written the other way and just as retention-bound.
        source = (
            "name: Verify\n"
            "on: [workflow_dispatch]\n"
            "jobs:\n"
            "  verify:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            '      - run: gh api "repos/${{ github.repository }}'
            '/actions/runs/${{ inputs.release_run_id }}"\n'
            '      - run: gh api "repos/${{ github.repository }}'
            '/actions/workflows/${{ inputs.workflow }}/runs?event=push"\n'
        )
        root = self.repo({".github/workflows/verify.yml": source})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_VIOLATIONS, out)
        self.assertIn(".github/workflows/verify.yml:7: workflow-run lookup by id", out)
        self.assertIn(".github/workflows/verify.yml:8: workflow-scoped runs-list", out)

    def test_commonjs_and_plain_js_are_scanned(self):
        # One policy must not be enforced or not enforced by file suffix. A
        # CommonJS module talking to the GitHub API is the same surface as an
        # ESM one.
        source = (
            "module.exports.stale = async function (octokit, owner, repo, runId) {\n"
            "  const r = await octokit.request(\n"
            "    `/repos/${owner}/${repo}/actions/runs/${runId}/jobs?per_page=100`);\n"
            "  return r.data;\n"
            "};\n"
        )
        for name in ("functions/github-app.js", "functions/legacy.cjs"):
            with self.subTest(name=name):
                root = self.repo({name: source})
                rc, out = self.run_guard(root)
                self.assertEqual(rc, EXIT_VIOLATIONS, out)
                self.assertIn(f"{name}:3:", out)

    def test_gh_run_cli_forms_are_reported(self):
        source = (
            "#!/usr/bin/env bash\n"
            "gh run view \"$OLD_RUN_ID\" --json conclusion\n"
            "gh run download \"$OLD_RUN_ID\" --name release-provenance\n"
            "gh run watch \"$OLD_RUN_ID\"\n"
            "gh run rerun \"$OLD_RUN_ID\"\n"
            "gh run list --workflow release.yml --limit 5\n"
        )
        root = self.repo({"scripts/inspect.sh": source})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_VIOLATIONS, out)
        for line in (2, 3, 4, 5):
            self.assertIn(f"scripts/inspect.sh:{line}: gh run CLI call", out)
        # `gh run list` asks what runs exist; it resolves no specific run.
        self.assertNotIn("scripts/inspect.sh:6:", out)

    def test_cross_run_artifact_retrieval_is_reported(self):
        source = (
            "name: Collect\n"
            "on: [workflow_dispatch]\n"
            "jobs:\n"
            "  collect:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/download-artifact@v4\n"
            "        with:\n"
            "          name: release-provenance\n"
            "          run-id: ${{ inputs.release_run_id }}\n"
            "      - uses: actions/download-artifact@v4\n"
            "        with:\n"
            "          run-id: ${{ github.run_id }}\n"
        )
        root = self.repo({".github/workflows/collect.yml": source})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_VIOLATIONS, out)
        self.assertIn(".github/workflows/collect.yml:10: cross-run artifact retrieval", out)
        # This run's own artifacts are not a past-run dependency.
        self.assertNotIn(".github/workflows/collect.yml:13:", out)

    def test_run_id_input_declaration_is_not_a_lookup(self):
        source = (
            "name: Collect\n"
            "on:\n"
            "  workflow_call:\n"
            "    inputs:\n"
            "      run-id:\n"
            "        required: true\n"
            "        type: string\n"
        )
        root = self.repo({".github/workflows/collect.yml": source})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_OK, out)

    def test_artifact_and_log_client_siblings_are_reported(self):
        source = (
            "export async function pull(github) {\n"
            "  await github.rest.actions.listWorkflowRunArtifacts({});\n"
            "  await github.rest.actions.downloadWorkflowRunLogs({});\n"
            "  await github.rest.actions.getWorkflowRunUsage({});\n"
            "}\n"
        )
        root = self.repo({"scripts/pull.mjs": source})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_VIOLATIONS, out)
        for line in (2, 3, 4):
            self.assertIn(f"scripts/pull.mjs:{line}:", out)

    def test_test_paths_are_skipped_by_default_and_the_skip_is_reported(self):
        root = self.repo({"scripts/test-verify.sh": RUN_ID_LOOKUP_SH})
        rc, out = self.run_guard(root)
        self.assertEqual(rc, EXIT_OK, out)
        self.assertSummary(out, "skipped as tests", "1")

    def test_include_tests_scans_test_paths(self):
        root = self.repo({"scripts/test-verify.sh": RUN_ID_LOOKUP_SH})
        rc, out = self.run_guard(root, "--include-tests")
        self.assertEqual(rc, EXIT_VIOLATIONS, out)
        self.assertIn("scripts/test-verify.sh:4:", out)


class AllowlistContract(GuardCase):
    def test_pinned_expression_passes(self):
        root = self.repo({"scripts/forensic.sh": FORENSIC_SH})
        name = self.allowlist(root, [{
            "file": "scripts/forensic.sh",
            "reason": "frozen forensic record of a resolved promotion incident",
            "owner": "infra-release",
            "allow_match": ["repos/${INFRA_REPO}/actions/runs/${ORIGIN_RUN_ID}"],
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_OK, out)
        self.assertSummary(out, "allowlisted files", "1 valid")

    def test_pinned_expression_does_not_exempt_a_second_lookup_on_the_line(self):
        source = FORENSIC_SH.replace(
            'echo "$origin_run_json"',
            'other="$(gh api "repos/${INFRA_REPO}/actions/runs/${OTHER_RUN_ID}")"',
        )
        root = self.repo({"scripts/forensic.sh": source})
        name = self.allowlist(root, [{
            "file": "scripts/forensic.sh",
            "reason": "frozen forensic record of a resolved promotion incident",
            "owner": "infra-release",
            "allow_match": ["repos/${INFRA_REPO}/actions/runs/${ORIGIN_RUN_ID}"],
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_VIOLATIONS, out)
        self.assertIn("${OTHER_RUN_ID}", out)

    def test_whole_file_exemption_must_be_explicit(self):
        root = self.repo({"scripts/verify.sh": RUN_ID_LOOKUP_SH})
        name = self.allowlist(root, [{
            "file": "scripts/verify.sh",
            "reason": "run forensics tool",
            "owner": "infra-release",
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_ALLOWLIST, out)
        self.assertIn("declares neither allow_match nor allow_file", out)

    def test_explicit_whole_file_exemption_is_honored_and_counted(self):
        root = self.repo({"scripts/verify.sh": RUN_ID_LOOKUP_SH})
        name = self.allowlist(root, [{
            "file": "scripts/verify.sh",
            "reason": "this script exists to inspect run history",
            "owner": "infra-release",
            "allow_file": True,
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_OK, out)
        self.assertSummary(out, "allowlisted files", "1 valid \\(1 whole-file\\)")

    def test_missing_reason_or_owner_fails(self):
        root = self.repo({"scripts/verify.sh": RUN_ID_LOOKUP_SH})
        name = self.allowlist(root, [{
            "file": "scripts/verify.sh",
            "allow_file": True,
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_ALLOWLIST, out)
        self.assertIn("missing required field(s): reason, owner", out)

    def test_unmatched_pin_fails_loud(self):
        root = self.repo({"scripts/forensic.sh": FORENSIC_SH})
        name = self.allowlist(root, [{
            "file": "scripts/forensic.sh",
            "reason": "frozen forensic record",
            "owner": "infra-release",
            "allow_match": ["repos/${INFRA_REPO}/actions/runs/${MOVED_SINCE}"],
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_ALLOWLIST, out)
        self.assertIn("occurs 0 time(s)", out)

    def test_pin_that_exempts_nothing_fails(self):
        root = self.repo({"scripts/forensic.sh": FORENSIC_SH})
        name = self.allowlist(root, [{
            "file": "scripts/forensic.sh",
            "reason": "frozen forensic record",
            "owner": "infra-release",
            "allow_match": ["ORIGIN_RUN_ID=29205793134"],
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_ALLOWLIST, out)
        self.assertIn("exempts nothing", out)

    def test_stale_path_fails(self):
        root = self.repo({"scripts/verify.sh": RUN_ID_LOOKUP_SH})
        name = self.allowlist(root, [{
            "file": "scripts/deleted-last-year.sh",
            "reason": "was a run forensics tool",
            "owner": "infra-release",
            "allow_file": True,
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_ALLOWLIST, out)
        self.assertIn("names a path that does not exist", out)

    def test_expired_entry_fails(self):
        root = self.repo({"scripts/verify.sh": RUN_ID_LOOKUP_SH})
        name = self.allowlist(root, [{
            "file": "scripts/verify.sh",
            "reason": "temporary while the fix is in flight",
            "owner": "infra-release",
            "allow_file": True,
            "expiration": "2020-01-01",
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_ALLOWLIST, out)
        self.assertIn("expired 2020-01-01", out)

    def test_misspelled_key_cannot_widen_an_exemption(self):
        root = self.repo({"scripts/verify.sh": RUN_ID_LOOKUP_SH})
        name = self.allowlist(root, [{
            "file": "scripts/verify.sh",
            "reason": "pinned expression",
            "owner": "infra-release",
            "allow_matches": ["repos/kinlab-ai/kin/actions/runs/${release_run_id}"],
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_ALLOWLIST, out)
        self.assertIn("unknown field(s): allow_matches", out)

    def test_duplicate_entry_fails(self):
        root = self.repo({"scripts/verify.sh": RUN_ID_LOOKUP_SH})
        entry = {
            "file": "scripts/verify.sh",
            "reason": "run forensics tool",
            "owner": "infra-release",
            "allow_file": True,
        }
        name = self.allowlist(root, [entry, dict(entry)])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_ALLOWLIST, out)
        self.assertIn("is duplicated", out)

    def test_prefix_only_pin_is_rejected(self):
        # A pin stopping at `actions/runs/` names no run id, so it would keep
        # exempting the line after someone repointed it at a different run.
        root = self.repo({"scripts/forensic.sh": FORENSIC_SH})
        name = self.allowlist(root, [{
            "file": "scripts/forensic.sh",
            "reason": "frozen forensic record",
            "owner": "infra-release",
            "allow_match": ["repos/${INFRA_REPO}/actions/runs/"],
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_ALLOWLIST, out)
        self.assertIn("names no run id", out)

    def test_documenting_the_exempted_expression_keeps_the_entry_valid(self):
        # Writing down why a lookup is frozen is the guard's own recommended
        # workflow. Quoting the expression in a comment beside it must not
        # invalidate the entry that exempts it.
        source = FORENSIC_SH.replace(
            "ORIGIN_RUN_ID=29205793134",
            "# frozen: repos/${INFRA_REPO}/actions/runs/${ORIGIN_RUN_ID} is the record\n"
            "ORIGIN_RUN_ID=29205793134",
        )
        root = self.repo({"scripts/forensic.sh": source})
        name = self.allowlist(root, [{
            "file": "scripts/forensic.sh",
            "reason": "frozen forensic record of a resolved promotion incident",
            "owner": "infra-release",
            "allow_match": ["repos/${INFRA_REPO}/actions/runs/${ORIGIN_RUN_ID}"],
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_OK, out)

    def test_pinning_a_test_path_announces_the_un_skip(self):
        source = (
            "#!/usr/bin/env bash\n"
            'gh api "repos/x/y/actions/runs/${PINNED_ID}"\n'
            'gh api "repos/x/y/actions/runs/${SIBLING_FIXTURE_ID}"\n'
        )
        root = self.repo({"scripts/test-policy.sh": source})
        name = self.allowlist(root, [{
            "file": "scripts/test-policy.sh",
            "reason": "asserts the shape of a production call",
            "owner": "infra-release",
            "allow_match": ["repos/x/y/actions/runs/${PINNED_ID}"],
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        # The sibling fixture line was invisible before the entry existed, so
        # the newly surfaced failure has to be traceable to the entry.
        self.assertEqual(rc, EXIT_VIOLATIONS, out)
        self.assertIn("test paths un-skipped", out)
        self.assertIn("scripts/test-policy.sh", out)
        self.assertIn("${SIBLING_FIXTURE_ID}", out)

    def test_missing_allowlist_file_fails_loud(self):
        root = self.repo({".github/workflows/verify.yml": CLEAN_WORKFLOW})
        rc, out = self.run_guard(root, "--allowlist", "no/such/allowlist.json")
        self.assertEqual(rc, EXIT_ALLOWLIST, out)
        self.assertIn("cannot read allowlist", out)

    def test_malformed_json_fails_loud(self):
        root = self.repo({".github/workflows/verify.yml": CLEAN_WORKFLOW})
        (Path(root) / "broken.json").write_text("{ not json", encoding="utf-8")
        rc, out = self.run_guard(root, "--allowlist", "broken.json")
        self.assertEqual(rc, EXIT_ALLOWLIST, out)
        self.assertIn("is not valid JSON", out)


class AllowlistErrorsNeverMaskTheScan(GuardCase):
    """The failure mode this guard was built to avoid.

    An allowlist error that aborts before the scan reports a clean tree while
    real violations sit behind it. Every case here proves the scan ran anyway,
    and that the two failures stay separable by exit code.
    """

    def test_broken_entry_still_reports_the_violation_it_names(self):
        root = self.repo({"scripts/verify.sh": RUN_ID_LOOKUP_SH})
        name = self.allowlist(root, [{
            "file": "scripts/verify.sh",
            "reason": "fix in flight",
            "owner": "infra-release",
            "allow_match": ["repos/kinlab-ai/kin/actions/runs/${moved_since}"],
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_ALLOWLIST, out)
        self.assertIn("scripts/verify.sh:4: workflow-run lookup by id", out)
        self.assertIn("occurs 0 time(s)", out)
        self.assertIn("exit: 3 (allowlist error; violations also found)", out)

    def test_broken_entry_does_not_suppress_another_file(self):
        root = self.repo({
            "scripts/verify.sh": RUN_ID_LOOKUP_SH,
            "scripts/sync.sh": RUNS_LIST_SH,
        })
        name = self.allowlist(root, [{
            "file": "scripts/gone.sh",
            "reason": "stale entry left behind",
            "owner": "infra-release",
            "allow_file": True,
        }])
        rc, out = self.run_guard(root, "--allowlist", name)
        self.assertEqual(rc, EXIT_ALLOWLIST, out)
        self.assertIn("scripts/verify.sh:4:", out)
        self.assertIn("scripts/sync.sh:3:", out)

    def test_the_three_failure_classes_use_three_exit_codes(self):
        clean = self.repo({".github/workflows/verify.yml": CLEAN_WORKFLOW})
        dirty = self.repo({"scripts/verify.sh": RUN_ID_LOOKUP_SH})
        broken_name = self.allowlist(clean, [{
            "file": "scripts/gone.sh",
            "reason": "stale entry left behind",
            "owner": "infra-release",
            "allow_file": True,
        }])
        codes = {
            "clean": self.run_guard(clean)[0],
            "violations": self.run_guard(dirty)[0],
            "allowlist": self.run_guard(clean, "--allowlist", broken_name)[0],
            "run-error": self.run_guard(tempfile.mkdtemp())[0],
        }
        self.assertEqual(
            codes,
            {"clean": EXIT_OK, "violations": EXIT_VIOLATIONS,
             "allowlist": EXIT_ALLOWLIST, "run-error": EXIT_RUN_ERROR},
        )


class Wiring(unittest.TestCase):
    """The shared checker is only shared if the workflow really calls it."""

    def test_reusable_workflow_invokes_the_checker(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(".kin-actions/scripts/workflow-run-lookup-guard.py", text)
        self.assertIn("workflow_call", text)
        self.assertIn("allowlist:", text)

    def test_reusable_workflow_reads_the_exit_code_directly(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("rc=$?", text)
        self.assertIn('exit "$rc"', text)
        # A missing helper tree has one cause worth naming, so the job says it
        # instead of failing as a Python import error.
        self.assertIn('if [ ! -f "$checker" ]', text)


class WorkflowStepBehavior(unittest.TestCase):
    """Executes the reusable workflow's step body the way GitHub does.

    GitHub runs a `run:` block with no explicit shell as `bash -e {0}`, and
    `set -uo pipefail` does not clear that inherited `-e`. Asserting the text
    `rc=$?` appears in the file certified a property the shell did not actually
    have: a non-zero checker exit ended the step before the classification ran,
    so the two branches that matter could never emit their annotation. These
    cases run the extracted body under the real shell against stub checkers.
    """

    def run_step(self, exit_code=None, **env_overrides):
        work = tempfile.mkdtemp(prefix="run-lookup-step-")
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        if exit_code is not None:
            checker_dir = Path(work) / ".kin-actions" / "scripts"
            checker_dir.mkdir(parents=True, exist_ok=True)
            (checker_dir / "workflow-run-lookup-guard.py").write_text(
                STUB_CHECKER.format(code=exit_code), encoding="utf-8"
            )
        script = Path(work) / "step.sh"
        script.write_text(
            extract_run_block(WORKFLOW.read_text(encoding="utf-8")), encoding="utf-8"
        )
        env = dict(
            os.environ,
            ALLOWLIST="",
            INCLUDE_TESTS="false",
            KIN_ACTIONS_REF="deadbeef",
        )
        env.update(env_overrides)
        return subprocess.run(
            ["bash", "-e", str(script)],
            cwd=work,
            text=True,
            capture_output=True,
            env=env,
        )

    def test_clean_run_propagates_zero_and_annotates_nothing(self):
        result = self.run_step(0)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("::error::", result.stdout)

    def test_violation_exit_survives_errexit_and_annotates(self):
        result = self.run_step(1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error::", result.stdout)
        self.assertIn("resolves a specific workflow run", result.stdout)

    def test_allowlist_exit_survives_errexit_and_annotates_differently(self):
        result = self.run_step(3)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("::error::", result.stdout)
        self.assertIn("allowlist is invalid", result.stdout)

    def test_run_error_exit_survives_errexit_and_annotates(self):
        result = self.run_step(4)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("could not run", result.stdout)

    def test_the_three_annotations_differ_from_each_other(self):
        messages = {
            code: self.run_step(code).stdout for code in (1, 3, 4)
        }
        self.assertEqual(len({m.strip() for m in messages.values()}), 3)

    def test_missing_checker_names_the_called_workflow_commit(self):
        result = self.run_step(None)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error::", result.stdout)
        self.assertIn("called-workflow commit", result.stdout)

    def test_inputs_reach_the_checker_as_argv(self):
        result = self.run_step(0, ALLOWLIST=".github/lookups.json", INCLUDE_TESTS="true")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--allowlist .github/lookups.json", result.stdout)
        self.assertIn("--include-tests", result.stdout)

    def test_empty_allowlist_input_passes_no_allowlist_flag(self):
        result = self.run_step(0)
        self.assertNotIn("--allowlist", result.stdout)
        self.assertNotIn("--include-tests", result.stdout)

    def test_reusable_workflow_passes_inputs_through_env(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        for name in ("ALLOWLIST:", "INCLUDE_TESTS:"):
            self.assertIn(name, text)
        run_block = text.split("run: |", 1)[1]
        self.assertNotIn("${{", run_block, "inputs must not be interpolated into the shell")

    def test_helper_source_is_the_exact_called_workflow_commit(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("repository: ${{ job.workflow_repository }}", text)
        self.assertIn("ref: ${{ job.workflow_sha }}", text)
        self.assertNotIn("ref: ${{ inputs.kin-actions-ref }}", text)

    def test_this_repo_runs_the_guard_on_itself(self):
        text = SELF_TEST.read_text(encoding="utf-8")
        self.assertIn("scripts/workflow-run-lookup-guard.py", text)

    def test_the_repo_itself_is_clean(self):
        # The release recovery helpers re-drive a run the caller already named,
        # so they hold reviewed allowlist entries. Running with the allowlist is
        # what this repository's own self-test does.
        allowlist = REPO / ".github" / "workflow-run-lookup-allowlist.json"
        self.assertTrue(allowlist.is_file(), allowlist)
        result = subprocess.run(
            [
                sys.executable,
                str(GUARD),
                "--root",
                str(REPO),
                "--allowlist",
                str(allowlist),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, EXIT_OK, result.stdout)

    def test_the_repo_claims_no_unreviewed_exemption(self):
        # Without the allowlist the same scan must still see the exemptions as
        # findings, so an entry can never quietly become invisible.
        result = subprocess.run(
            [sys.executable, str(GUARD), "--root", str(REPO)],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, EXIT_OK, result.stdout)
        for named in (
            "scripts/recover-registry-publish.py",
            "scripts/recover-release-pr-checks.py",
            "scripts/wait-tag-release-run.sh",
        ):
            self.assertIn(named, result.stdout)


if __name__ == "__main__":
    unittest.main()
