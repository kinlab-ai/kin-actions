#!/usr/bin/env python3
"""Validate and plan the version-bound kin-actions consumer rollout."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path, PurePosixPath


SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PROTOCOL_KEYS = {
    "minimum_version",
    "pilot_repository",
    "pin_branch",
    "required_check_app_id",
    "main_workflow",
}
TOP_LEVEL_KEYS = {"schema", "protocol", "rollout", "repositories"}
STAGE_KEYS = {"id", "repositories"}
REPOSITORY_KEYS = {"kind", "workflow_paths"}
CARGO_REPOSITORIES = {
    "kinlab-ai/kin-blobs",
    "kinlab-ai/kin-search",
    "kinlab-ai/kin-vector",
    "kinlab-ai/kin-infer",
    "kinlab-ai/kin-model",
    "kinlab-ai/kin-db",
    "kinlab-ai/kin-vfs",
    "kinlab-ai/kin-lsp",
}
EXPECTED_REPOSITORIES = {
    "kinlab-ai/kin": {
        "kind": "other",
        "workflow_paths": [
            ".github/workflows/approve-to-merge.yml",
            ".github/workflows/merge-queue-ejection-notice.yml",
            ".github/workflows/notify-approver.yml",
        ],
    },
    "kinlab-ai/kin-bench": {
        "kind": "other",
        "workflow_paths": [
            ".github/workflows/approve-to-merge.yml",
            ".github/workflows/kin-dependency-wave.yml",
            ".github/workflows/merge-queue-ejection-notice.yml",
            ".github/workflows/notify-approver.yml",
        ],
    },
    "kinlab-ai/kinlab": {
        "kind": "other",
        "workflow_paths": [
            ".github/workflows/ci.yml",
            ".github/workflows/merge-queue-ejection-notice.yml",
            ".github/workflows/notify-approver.yml",
        ],
    },
    "kinlab-ai/kin-blobs": {
        "kind": "cargo_release",
        "workflow_paths": [
            ".github/workflows/registry-publish.yml",
            ".github/workflows/scheduled-failure-alarm.yml",
        ],
    },
    "kinlab-ai/kin-search": {
        "kind": "cargo_release",
        "workflow_paths": [
            ".github/workflows/merge-queue-ejection-notice.yml",
            ".github/workflows/registry-publish.yml",
        ],
    },
    "kinlab-ai/kin-vector": {
        "kind": "cargo_release",
        "workflow_paths": [
            ".github/workflows/registry-publish.yml",
            ".github/workflows/scheduled-failure-alarm.yml",
        ],
    },
    "kinlab-ai/kin-infer": {
        "kind": "cargo_release",
        "workflow_paths": [
            ".github/workflows/merge-queue-ejection-notice.yml",
            ".github/workflows/registry-publish.yml",
        ],
    },
    "kinlab-ai/kin-model": {
        "kind": "cargo_release",
        "workflow_paths": [
            ".github/workflows/kin-dependency-wave.yml",
            ".github/workflows/merge-queue-ejection-notice.yml",
            ".github/workflows/registry-publish.yml",
            ".github/workflows/release-recovery.yml",
        ],
    },
    "kinlab-ai/kin-db": {
        "kind": "cargo_release",
        "workflow_paths": [
            ".github/workflows/kin-dependency-wave.yml",
            ".github/workflows/registry-publish.yml",
            ".github/workflows/scheduled-failure-alarm.yml",
        ],
    },
    "kinlab-ai/kin-vfs": {
        "kind": "cargo_release",
        "workflow_paths": [
            ".github/workflows/kin-dependency-wave.yml",
            ".github/workflows/merge-queue-ejection-notice.yml",
            ".github/workflows/registry-publish.yml",
        ],
    },
    "kinlab-ai/kin-lsp": {
        "kind": "cargo_release",
        "workflow_paths": [
            ".github/workflows/kin-dependency-wave.yml",
            ".github/workflows/merge-queue-ejection-notice.yml",
            ".github/workflows/registry-publish.yml",
        ],
    },
    "kinlab-ai/kin-bench-spec": {
        "kind": "other",
        "workflow_paths": [".github/workflows/merge-queue-ejection-notice.yml"],
    },
    "kinlab-ai/kin-editor": {
        "kind": "other",
        "workflow_paths": [".github/workflows/merge-queue-ejection-notice.yml"],
    },
    "kinlab-ai/kin-infra": {
        "kind": "other",
        "workflow_paths": [".github/workflows/ci.yml"],
    },
    "kinlab-ai/homebrew-kin": {
        "kind": "other",
        "workflow_paths": [".github/workflows/ci.yml"],
    },
}
EXPECTED_ROLLOUT = [
    {"id": "pilot", "repositories": ["kinlab-ai/kin-blobs"]},
    {
        "id": "primitives",
        "repositories": [
            "kinlab-ai/kin-search",
            "kinlab-ai/kin-vector",
            "kinlab-ai/kin-infer",
        ],
    },
    {"id": "model", "repositories": ["kinlab-ai/kin-model"]},
    {"id": "database", "repositories": ["kinlab-ai/kin-db"]},
    {
        "id": "surfaces",
        "repositories": ["kinlab-ai/kin-vfs", "kinlab-ai/kin-lsp"],
    },
    {
        "id": "other-consumers",
        "repositories": [
            "kinlab-ai/kin",
            "kinlab-ai/kin-bench",
            "kinlab-ai/kinlab",
            "kinlab-ai/kin-bench-spec",
            "kinlab-ai/kin-editor",
            "kinlab-ai/kin-infra",
            "kinlab-ai/homebrew-kin",
        ],
    },
]


class RolloutError(RuntimeError):
    """The consumer manifest or rollout state is not authoritative."""


def _load(name: str, filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_version(raw: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(raw)
    if not match:
        raise RolloutError(f"expected stable numeric SemVer, got {raw!r}")
    return tuple(int(part) for part in match.groups())


def _exact_keys(value: object, expected: set[str], location: str) -> dict:
    if not isinstance(value, dict):
        raise RolloutError(f"{location} must be an object")
    actual = set(value)
    if actual != expected:
        raise RolloutError(
            f"{location} keys must be exactly {sorted(expected)}; "
            f"found {sorted(actual)}"
        )
    return value


def _validate_path(repository: str, raw: object) -> str:
    if not isinstance(raw, str):
        raise RolloutError(f"{repository}: workflow path must be a string")
    pure = PurePosixPath(raw)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or not raw.startswith(".github/workflows/")
        or pure.suffix not in {".yml", ".yaml"}
        or pure.as_posix() != raw
    ):
        raise RolloutError(f"{repository}: unsafe workflow path {raw!r}")
    return raw


def load_manifest(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RolloutError(f"manifest is not a regular file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RolloutError(f"invalid consumer manifest: {exc}") from exc
    _exact_keys(data, TOP_LEVEL_KEYS, "manifest")
    if data["schema"] != 2:
        raise RolloutError("consumer manifest must use schema 2")

    protocol = _exact_keys(data["protocol"], PROTOCOL_KEYS, "protocol")
    if parse_version(protocol["minimum_version"]) != (0, 1, 34):
        raise RolloutError("protocol minimum_version must remain exactly 0.1.34")
    if protocol["pilot_repository"] != "kinlab-ai/kin-blobs":
        raise RolloutError("the only pilot authority is kinlab-ai/kin-blobs")
    if protocol["pin_branch"] != "automation/kin-actions-pin-next":
        raise RolloutError("pin_branch must preserve the protected automation branch")
    if protocol["required_check_app_id"] != 15368:
        raise RolloutError("required checks must remain bound to GitHub Actions App 15368")
    if protocol["main_workflow"] != "registry-publish.yml":
        raise RolloutError("cargo main proof must remain bound to registry-publish.yml")

    repositories = data["repositories"]
    if not isinstance(repositories, dict) or not repositories:
        raise RolloutError("repositories must be a non-empty object")
    validated_repositories: dict[str, dict] = {}
    all_paths = 0
    for repository, raw_spec in repositories.items():
        if not isinstance(repository, str) or not REPOSITORY_RE.fullmatch(repository):
            raise RolloutError(f"unsafe repository name: {repository!r}")
        spec = _exact_keys(raw_spec, REPOSITORY_KEYS, f"repository {repository}")
        if spec["kind"] not in {"cargo_release", "other"}:
            raise RolloutError(f"{repository}: invalid repository kind")
        raw_paths = spec["workflow_paths"]
        if not isinstance(raw_paths, list) or not raw_paths:
            raise RolloutError(f"{repository}: workflow_paths must be non-empty")
        paths = [_validate_path(repository, raw) for raw in raw_paths]
        if len(paths) != len(set(paths)):
            raise RolloutError(f"{repository}: duplicate workflow path")
        validated_repositories[repository] = {
            "kind": spec["kind"],
            "workflow_paths": paths,
        }
        all_paths += len(paths)

    cargo = {
        repository
        for repository, spec in validated_repositories.items()
        if spec["kind"] == "cargo_release"
    }
    if cargo != CARGO_REPOSITORIES:
        raise RolloutError(
            "cargo_release repositories must equal the eight release callers"
        )
    if validated_repositories != EXPECTED_REPOSITORIES:
        raise RolloutError(
            "schema 2 repositories and workflow paths must equal the exact live inventory"
        )

    rollout = data["rollout"]
    if not isinstance(rollout, list) or not rollout:
        raise RolloutError("rollout must be a non-empty array")
    stage_ids: set[str] = set()
    ordered: list[str] = []
    for index, raw_stage in enumerate(rollout):
        stage = _exact_keys(raw_stage, STAGE_KEYS, f"rollout stage {index}")
        stage_id = stage["id"]
        members = stage["repositories"]
        if not isinstance(stage_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", stage_id):
            raise RolloutError(f"rollout stage {index}: unsafe id")
        if stage_id in stage_ids:
            raise RolloutError(f"duplicate rollout stage id: {stage_id}")
        stage_ids.add(stage_id)
        if not isinstance(members, list) or not members:
            raise RolloutError(f"rollout stage {stage_id}: repositories must be non-empty")
        for repository in members:
            if repository not in validated_repositories:
                raise RolloutError(
                    f"rollout stage {stage_id}: unknown repository {repository!r}"
                )
            if repository in ordered:
                raise RolloutError(f"repository appears in multiple stages: {repository}")
            ordered.append(repository)
    if rollout[0] != {
        "id": "pilot",
        "repositories": [protocol["pilot_repository"]],
    }:
        raise RolloutError("the first stage must be the one-repository kin-blobs pilot")
    if rollout != EXPECTED_ROLLOUT:
        raise RolloutError("rollout stages must preserve the exact bottom-up sequence")
    if set(ordered) != set(validated_repositories):
        missing = sorted(set(validated_repositories) - set(ordered))
        raise RolloutError("repositories missing from rollout: " + ", ".join(missing))
    if len(validated_repositories) != 15 or all_paths != 35:
        raise RolloutError("schema 2 activation requires the exact 15-repository, 35-path inventory")

    return {
        "schema": 2,
        "protocol": dict(protocol),
        "rollout": [dict(stage) for stage in rollout],
        "repositories": validated_repositories,
    }


def rollout_sequence(manifest: dict) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for stage in manifest["rollout"]:
        for repository in stage["repositories"]:
            result.append(
                {
                    "stage": stage["id"],
                    "repository": repository,
                    "kind": manifest["repositories"][repository]["kind"],
                }
            )
    return result


def checkout_directory(root: Path, repository: str) -> Path:
    return root / repository.replace("/", "__")


def preflight_checkouts(
    manifest: dict, checkouts_root: Path, target_version: str
) -> dict:
    target = parse_version(target_version)
    pins = _load("rollout_pin_updater", "update-kin-actions-pins.py")
    states: list[dict[str, object]] = []
    for item in rollout_sequence(manifest):
        repository = item["repository"]
        root = checkout_directory(checkouts_root, repository)
        if root.is_symlink() or not root.is_dir():
            raise RolloutError(f"missing regular preflight checkout for {repository}: {root}")
        expected = manifest["repositories"][repository]["workflow_paths"]
        try:
            discovered = pins.discover_pin_paths(root, require_stable=False)
        except pins.PinUpdateError as exc:
            raise RolloutError(f"{repository}: {exc}") from exc
        if discovered != sorted(expected):
            missing = sorted(set(discovered) - set(expected))
            stale = sorted(set(expected) - set(discovered))
            raise RolloutError(
                f"{repository}: manifest inventory drift; "
                f"unmanifested={missing}; stale={stale}"
            )
        refs: set[str] = set()
        for relative in expected:
            try:
                semantic = pins.semantic_kin_actions_uses(root / relative, relative)
            except pins.PinUpdateError as exc:
                raise RolloutError(f"{repository}: {exc}") from exc
            for _line, value in semantic:
                refs.add(value.rsplit("@", 1)[-1])
        if len(refs) != 1 or not pins.STABLE_TAG_RE.fullmatch(next(iter(refs), "")):
            states.append(
                {
                    **item,
                    "current_ref": sorted(refs),
                    "relation": "blocked",
                    "blocker": "pins must first migrate to one stable version tag",
                }
            )
            continue
        current_ref = refs.pop()
        current_version = current_ref.removeprefix("v")
        current = parse_version(current_version)
        if current > target:
            raise RolloutError(
                f"{repository}: refusing target v{target_version} because main is newer at v{current_version}"
            )
        try:
            # Planning is read-only and proves every late-stage consumer can be
            # rewritten completely before the controller mutates the pilot.
            pins.plan_updates(
                root=root,
                paths=expected,
                target_version=target_version,
            )
        except pins.PinUpdateError as exc:
            raise RolloutError(f"{repository}: pin rewrite preflight failed: {exc}") from exc
        states.append(
            {
                **item,
                "current_version": current_version,
                "relation": "target" if current == target else "behind",
            }
        )
    return {"target_version": target_version, "repositories": states}


def plan_rollout(manifest: dict, inventory: dict, proofs: dict[str, str]) -> dict:
    expected = rollout_sequence(manifest)
    actual = inventory.get("repositories")
    if not isinstance(actual, list) or len(actual) != len(expected):
        raise RolloutError("inventory does not cover the complete rollout")
    by_repository = {
        item.get("repository"): item for item in actual if isinstance(item, dict)
    }
    if set(by_repository) != {item["repository"] for item in expected}:
        raise RolloutError("inventory repository set differs from the rollout")

    for item in expected:
        repository = item["repository"]
        state = by_repository[repository]
        relation = state.get("relation")
        if relation == "behind":
            return {"status": "reconcile", **item}
        if relation == "blocked":
            blocker = state.get("blocker")
            if not isinstance(blocker, str) or not blocker:
                raise RolloutError(f"{repository}: blocked inventory lacks an exact reason")
            return {"status": "blocked-activation", "blocker": blocker, **item}
        if relation != "target":
            raise RolloutError(f"{repository}: invalid inventory relation {relation!r}")
        proof = proofs.get(repository, "waiting")
        if proof != "proven":
            if proof not in {"waiting", "blocked"}:
                raise RolloutError(f"{repository}: invalid rollout proof state {proof!r}")
            scope = "main" if item["kind"] == "cargo_release" else "landing"
            return {"status": f"{proof}-{scope}-proof", **item}
    return {"status": "complete"}


def _json_file(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RolloutError(f"invalid JSON state file {path}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--manifest", type=Path, required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--manifest", type=Path, required=True)
    preflight.add_argument("--checkouts-root", type=Path, required=True)
    preflight.add_argument("--target-version", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--manifest", type=Path, required=True)
    plan.add_argument("--inventory", type=Path, required=True)
    plan.add_argument("--proofs", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest.resolve())
        if args.command == "validate":
            result = {
                "repositories": len(manifest["repositories"]),
                "workflow_paths": sum(
                    len(spec["workflow_paths"])
                    for spec in manifest["repositories"].values()
                ),
                "sequence": rollout_sequence(manifest),
            }
        elif args.command == "preflight":
            result = preflight_checkouts(
                manifest, args.checkouts_root.resolve(), args.target_version
            )
        else:
            inventory = _json_file(args.inventory)
            proofs = _json_file(args.proofs)
            if not isinstance(inventory, dict) or not isinstance(proofs, dict):
                raise RolloutError("inventory and proofs must be JSON objects")
            result = plan_rollout(manifest, inventory, proofs)
    except RolloutError as exc:
        print(f"workflow-pin rollout failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
