#!/usr/bin/env python3
"""Resolve the exact merged pin PR whose commit reached consumer main."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path


SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GOOD_CONCLUSIONS = {"success", "skipped"}
GOOD_REQUIRED_BUCKETS = {"pass", "skipping"}
PIN_DIFF_RE = re.compile(
    r"^(?P<prefix>[ \t]*(?:-[ \t]+)?uses:[ \t]*"
    r"(?:kinlab-ai|firelock-ai)/kin-actions/\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml@v)"
    r"(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*))(?P<suffix>[ \t]*(?:#.*)?)$"
)


def _load_protection():
    path = Path(__file__).with_name("workflow-pin-protection.py")
    spec = importlib.util.spec_from_file_location(
        "workflow_pin_protection_for_landing", path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


protection_guard = _load_protection()


class LandingError(RuntimeError):
    """Merged-PR evidence cannot authorize rollout progression."""


def exact_merged_pulls(
    *,
    pulls: list[dict],
    repository: str,
    base: str,
    head_branch: str,
    target_version: str,
) -> list[dict]:
    """Return only first-party merged PRs with the versioned authority title."""

    title = f"chore(ci): pin kin-actions v{target_version}"
    exact = []
    for pr in pulls:
        head = pr.get("head")
        head_repo = head.get("repo") if isinstance(head, dict) else None
        base_data = pr.get("base")
        if (
            pr.get("state") == "closed"
            and pr.get("merged_at")
            and pr.get("title") == title
            and isinstance(head, dict)
            and head.get("ref") == head_branch
            and isinstance(head_repo, dict)
            and head_repo.get("full_name") == repository
            and isinstance(base_data, dict)
            and base_data.get("ref") == base
        ):
            exact.append(pr)
    return exact


def evaluate_landing(
    *,
    pulls: list[dict],
    compare: dict | None,
    repository: str,
    base: str,
    head_branch: str,
    target_version: str,
    landed_files: dict[str, tuple[bytes, bytes]],
    check_runs: list[dict],
    required_checks: list[dict],
    repository_settings: dict,
    status_checks: dict,
    kind: str,
    required_app_id: int,
    allowed_paths: list[str],
) -> dict:
    exact = exact_merged_pulls(
        pulls=pulls,
        repository=repository,
        base=base,
        head_branch=head_branch,
        target_version=target_version,
    )
    if not exact:
        return {"status": "waiting", "reason": "exact generated pin PR is not merged"}
    if len(exact) != 1:
        raise LandingError("multiple merged PRs claim the exact versioned pin authority")
    pr = exact[0]
    number = pr.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise LandingError("merged pin PR lacks an exact positive number")
    merge_sha = pr.get("merge_commit_sha")
    if not isinstance(merge_sha, str) or not SHA_RE.fullmatch(merge_sha):
        raise LandingError("merged pin PR lacks an exact merge commit SHA")
    if not isinstance(compare, dict):
        raise LandingError("merge ancestry comparison is missing")
    if compare.get("status") not in {"ahead", "identical"}:
        raise LandingError("exact pin merge is not an ancestor of current main")
    base_commit = compare.get("base_commit")
    if not isinstance(base_commit, dict) or base_commit.get("sha") != merge_sha:
        raise LandingError("comparison base is not the exact pin merge")

    head = pr.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(head_sha, str) or not SHA_RE.fullmatch(head_sha):
        raise LandingError("merged pin PR lacks an exact generated head SHA")
    allowed = set(allowed_paths)
    if not allowed or len(allowed) != len(allowed_paths):
        raise LandingError("manifest path allowlist is empty or duplicated")
    if not isinstance(landed_files, dict) or not landed_files:
        raise LandingError("merged pin PR has no exact landed Git-object changes")
    if not set(landed_files).issubset(allowed):
        raise LandingError("merged pin PR changed a non-manifest path")
    changed_paths: list[str] = []
    target = tuple(int(part) for part in target_version.split("."))
    for filename, blobs in landed_files.items():
        if (
            not isinstance(filename, str)
            or not isinstance(blobs, tuple)
            or len(blobs) != 2
            or not all(isinstance(blob, bytes) for blob in blobs)
        ):
            raise LandingError("merged pin PR Git-object evidence is malformed")
        try:
            before = blobs[0].decode("utf-8")
            after = blobs[1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LandingError("merged pin PR changed a non-UTF-8 workflow") from exc
        before_lines = before.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        if len(before_lines) != len(after_lines):
            raise LandingError("merged pin PR changes bytes beyond canonical pins")
        rewrites = 0
        for before_line, after_line in zip(before_lines, after_lines, strict=True):
            if before_line == after_line:
                continue
            before_text, before_ending = _split_line_ending(before_line)
            after_text, after_ending = _split_line_ending(after_line)
            before_match = PIN_DIFF_RE.fullmatch(before_text)
            after_match = PIN_DIFF_RE.fullmatch(after_text)
            if (
                not before_match
                or not after_match
                or before_ending != after_ending
                or before_match.group("prefix") != after_match.group("prefix")
                or before_match.group("suffix") != after_match.group("suffix")
                or after_match.group("version") != target_version
            ):
                raise LandingError("merged pin PR changes bytes beyond canonical pins")
            removed = tuple(
                int(part) for part in before_match.group("version").split(".")
            )
            if removed >= target:
                raise LandingError(
                    "merged pin PR must replace an older stable version with the target"
                )
            rewrites += 1
        if rewrites == 0:
            raise LandingError("merged pin PR contains an unchanged manifest path")
        changed_paths.append(filename)

    activation = protection_guard.validate_protection(
        repository_settings=repository_settings,
        status_checks=status_checks,
        repository=repository,
        kind=kind,
        required_app_id=required_app_id,
    )
    if activation["default_branch"] != base:
        raise LandingError("merged pin PR base is not the live protected default branch")
    expected_apps = activation["required_checks"]
    if not check_runs:
        raise LandingError("merged pin PR has no exact-head check runs")
    names: dict[str, dict] = {}
    for check in check_runs:
        name = check.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise LandingError("merged pin PR check-run names are missing or duplicated")
        names[name] = check
        if check.get("status") != "completed" or check.get("conclusion") not in GOOD_CONCLUSIONS:
            raise LandingError(f"merged pin PR exact-head check is not green: {name}")
    required_names: set[str] = set()
    required_buckets: dict[str, str] = {}
    for check in required_checks:
        name = check.get("name")
        bucket = check.get("bucket")
        if not isinstance(name, str) or not name or name in required_names:
            raise LandingError("merged pin PR required-check names are missing or duplicated")
        if bucket not in GOOD_REQUIRED_BUCKETS:
            raise LandingError(f"merged pin PR required check is not green: {name}")
        if name not in names:
            raise LandingError(f"merged pin PR required check has no exact-head run: {name}")
        required_names.add(name)
        required_buckets[name] = bucket
    if required_names != set(expected_apps):
        raise LandingError("merged pin PR checks differ from live strict protection")
    for name, app_id in expected_apps.items():
        app = names[name].get("app")
        if not isinstance(app, dict) or app.get("id") != app_id:
            raise LandingError(f"merged pin PR required check has wrong App: {name}")
    if kind == "cargo_release":
        for name in protection_guard.CARGO_CONTEXTS:
            if names[name].get("conclusion") != "success" or required_buckets.get(name) != "pass":
                raise LandingError(f"merged pin PR Cargo proof did not pass: {name}")
    return {
        "status": "proven",
        "pr": number,
        "head_sha": head_sha,
        "merge_sha": merge_sha,
        "changed_paths": sorted(changed_paths),
    }


def _split_line_ending(line: str) -> tuple[str, str]:
    for ending in ("\r\n", "\n", "\r"):
        if line.endswith(ending):
            return line[: -len(ending)], ending
    return line, ""


def _json_command(command: list[str], *, allow_nonzero: bool = False):
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0 and not allow_nonzero:
        raise LandingError(
            f"command failed ({result.returncode}): {' '.join(command)}: "
            f"{result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout or "[]"), result.returncode
    except json.JSONDecodeError as exc:
        raise LandingError(
            f"command returned invalid JSON: {' '.join(command)}"
        ) from exc


def _bytes_command(command: list[str], *, accepted: set[int] = {0}) -> bytes:
    result = subprocess.run(command, check=False, capture_output=True)
    if result.returncode not in accepted:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise LandingError(
            f"command failed ({result.returncode}): {' '.join(command)}: {stderr}"
        )
    return result.stdout


def _origin_repository(raw: str) -> str | None:
    candidates = (
        re.fullmatch(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?", raw),
        re.fullmatch(r"ssh://git@github\.com/([^/]+/[^/]+?)(?:\.git)?", raw),
        re.fullmatch(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?", raw),
    )
    for match in candidates:
        if match:
            return match.group(1)
    return None


def _read_landed_object_changes(
    resolved: Path, merge_sha: str
) -> dict[str, tuple[bytes, bytes]]:
    """Return the exact one-parent commit blobs without REST patch truncation."""

    lineage = _bytes_command(
        ["git", "-C", str(resolved), "rev-list", "--parents", "-n", "1", merge_sha]
    ).decode("ascii", errors="strict").strip().split()
    if len(lineage) != 2 or lineage[0] != merge_sha:
        raise LandingError("pin landing must be one exact first-parent commit")
    parent = lineage[1]
    raw_names = _bytes_command(
        [
            "git",
            "-C",
            str(resolved),
            "diff-tree",
            "--no-commit-id",
            "--name-status",
            "-r",
            "-z",
            parent,
            merge_sha,
        ]
    )
    fields = raw_names.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if not fields or len(fields) % 2:
        raise LandingError("landed Git-object path evidence is empty or malformed")
    changes: dict[str, tuple[bytes, bytes]] = {}
    for index in range(0, len(fields), 2):
        try:
            status = fields[index].decode("ascii", errors="strict")
            path = fields[index + 1].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise LandingError("landed Git-object path is not canonical text") from exc
        if status != "M" or path in changes:
            raise LandingError("pin landing contains a non-modified or duplicate path")
        before = _bytes_command(
            ["git", "-C", str(resolved), "show", f"{parent}:{path}"]
        )
        after = _bytes_command(
            ["git", "-C", str(resolved), "show", f"{merge_sha}:{path}"]
        )
        changes[path] = (before, after)
    return changes


def landed_git_object_changes(
    *,
    checkout: Path,
    repository: str,
    base: str,
    default_head: str,
    merge_sha: str,
) -> dict[str, tuple[bytes, bytes]]:
    """Read the exact first-parent landed diff from the consumer clone."""

    if checkout.is_symlink() or not checkout.is_dir():
        raise LandingError("consumer checkout is not a regular directory")
    resolved = checkout.resolve()
    top = _bytes_command(
        ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"]
    ).decode("utf-8", errors="strict").strip()
    if Path(top).resolve() != resolved:
        raise LandingError("consumer checkout is not its exact Git top level")
    git_dir = resolved / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise LandingError("consumer checkout lacks a regular Git directory")
    origin = _bytes_command(
        ["git", "-C", str(resolved), "remote", "get-url", "origin"]
    ).decode("utf-8", errors="strict").strip()
    if _origin_repository(origin) != repository:
        raise LandingError("consumer checkout origin differs from the target repository")
    local_head = _bytes_command(
        ["git", "-C", str(resolved), "rev-parse", "HEAD"]
    ).decode("ascii", errors="strict").strip()
    if local_head != default_head:
        raise LandingError("consumer checkout moved from the exact default head")

    shallow = _bytes_command(
        ["git", "-C", str(resolved), "rev-parse", "--is-shallow-repository"]
    ).decode("ascii", errors="strict").strip()
    fetch = ["git", "-C", str(resolved), "fetch", "--no-tags", "--filter=blob:none"]
    if shallow == "true":
        fetch.append("--unshallow")
    elif shallow != "false":
        raise LandingError("consumer checkout returned an invalid shallow state")
    fetch.extend(["origin", base])
    _bytes_command(fetch)
    remote_head = _bytes_command(
        ["git", "-C", str(resolved), "rev-parse", f"refs/remotes/origin/{base}"]
    ).decode("ascii", errors="strict").strip()
    if remote_head != default_head:
        raise LandingError("consumer default branch moved during landed-object proof")
    _bytes_command(
        [
            "git",
            "-C",
            str(resolved),
            "merge-base",
            "--is-ancestor",
            merge_sha,
            default_head,
        ]
    )
    return _read_landed_object_changes(resolved, merge_sha)


def fetch_pulls(repository: str, base: str, head_branch: str) -> list[dict]:
    owner = repository.split("/", 1)[0]
    pages, _ = _json_command(
        [
            "gh",
            "api",
            "--method",
            "GET",
            "--paginate",
            "--slurp",
            f"repos/{repository}/pulls",
            "-f",
            "state=closed",
            "-f",
            f"base={base}",
            "-f",
            f"head={owner}:{head_branch}",
            "-f",
            "per_page=100",
        ]
    )
    pulls: list[dict] = []
    for page in pages:
        if not isinstance(page, list):
            raise LandingError("pull-request pagination returned an invalid page")
        pulls.extend(page)
    return pulls


def fetch_merged_check_evidence(
    repository: str, pr_number: int, head_sha: str
) -> tuple[list[dict], list[dict]]:
    check_pages, _ = _json_command(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repository}/commits/{head_sha}/check-runs?filter=latest&per_page=100",
        ]
    )
    check_runs: list[dict] = []
    for page in check_pages:
        if not isinstance(page, dict) or not isinstance(page.get("check_runs"), list):
            raise LandingError("merged pin PR check pagination returned an invalid page")
        check_runs.extend(page["check_runs"])
    required, code = _json_command(
        [
            "gh",
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            repository,
            "--required",
            "--json",
            "name,bucket,state,workflow",
        ],
        allow_nonzero=True,
    )
    if code not in {0, 1, 8} or not isinstance(required, list):
        raise LandingError("merged pin PR required-check query failed")
    return check_runs, required


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head-branch", required=True)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--default-head", required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--kind", choices=("cargo_release", "other"), required=True)
    parser.add_argument("--required-app-id", type=int, required=True)
    parser.add_argument("--allowed-path", action="append", default=[])
    args = parser.parse_args(argv)
    if not os.environ.get("GH_TOKEN"):
        print("workflow-pin landing proof failed: GH_TOKEN is required", file=sys.stderr)
        return 1
    if (
        not REPOSITORY_RE.fullmatch(args.repository)
        or not SEMVER_RE.fullmatch(args.target_version)
        or not SHA_RE.fullmatch(args.default_head)
    ):
        print("workflow-pin landing proof failed: unsafe input", file=sys.stderr)
        return 1
    try:
        pulls = fetch_pulls(args.repository, args.base, args.head_branch)
        candidates = exact_merged_pulls(
            pulls=pulls,
            repository=args.repository,
            base=args.base,
            head_branch=args.head_branch,
            target_version=args.target_version,
        )
        compare = None
        landed_files: dict[str, tuple[bytes, bytes]] = {}
        check_runs: list[dict] = []
        required_checks: list[dict] = []
        if len(candidates) == 1:
            merge_sha = candidates[0].get("merge_commit_sha")
            if isinstance(merge_sha, str) and SHA_RE.fullmatch(merge_sha):
                compare, _ = _json_command(
                    [
                        "gh",
                        "api",
                        f"repos/{args.repository}/compare/{merge_sha}...{args.default_head}",
                    ]
                )
            head = candidates[0].get("head")
            head_sha = head.get("sha") if isinstance(head, dict) else None
            number = candidates[0].get("number")
            if (
                isinstance(head_sha, str)
                and SHA_RE.fullmatch(head_sha)
                and isinstance(number, int)
                and not isinstance(number, bool)
                and number > 0
            ):
                check_runs, required_checks = fetch_merged_check_evidence(
                    args.repository, number, head_sha
                )
                landed_files = landed_git_object_changes(
                    checkout=args.checkout,
                    repository=args.repository,
                    base=args.base,
                    default_head=args.default_head,
                    merge_sha=merge_sha,
                )
        settings, status_checks = protection_guard.fetch_live(args.repository)
        result = evaluate_landing(
            pulls=pulls,
            compare=compare,
            repository=args.repository,
            base=args.base,
            head_branch=args.head_branch,
            target_version=args.target_version,
            landed_files=landed_files,
            check_runs=check_runs,
            required_checks=required_checks,
            repository_settings=settings,
            status_checks=status_checks,
            kind=args.kind,
            required_app_id=args.required_app_id,
            allowed_paths=args.allowed_path,
        )
    except (LandingError, protection_guard.ProtectionError) as exc:
        print(f"workflow-pin landing proof failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
