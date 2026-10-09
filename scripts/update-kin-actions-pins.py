#!/usr/bin/env python3
"""Update exact kin-actions workflow pins in one allowlisted consumer checkout."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path, PurePosixPath


SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
PIN_RE = re.compile(
    r"(?P<prefix>uses:\s*"
    r"(?:kinlab-ai|firelock-ai)/kin-actions/\.github/workflows/"
    r"[A-Za-z0-9_.-]+\.ya?ml@v)"
    r"(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*))"
)
KIN_ACTIONS_USE_RE = re.compile(
    r"^(?:kinlab-ai|firelock-ai)/kin-actions/\.github/workflows/"
    r"[A-Za-z0-9_.-]+\.ya?ml@(?P<ref>.+)$"
)
STABLE_TAG_RE = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)


def _load(name: str, filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_semantic = _load("pin_semantic_uses", "check-release-workflow-pins.py")


class PinUpdateError(RuntimeError):
    """A manifest or consumer invariant failed."""


def parse_version(raw: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(raw.strip())
    if not match:
        raise PinUpdateError(f"expected stable numeric SemVer, got {raw!r}")
    return tuple(int(part) for part in match.groups())


def load_consumer_paths(manifest: Path, repository: str) -> list[str]:
    if manifest.is_symlink() or not manifest.is_file():
        raise PinUpdateError(f"manifest is not a regular file: {manifest}")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PinUpdateError(f"invalid consumer manifest: {exc}") from exc
    if data.get("schema") == 1 and isinstance(data.get("repositories"), dict):
        raw_paths = data["repositories"].get(repository)
    elif data.get("schema") == 2:
        rollout = _load("pin_rollout_manifest", "workflow-pin-rollout.py")
        planned_path: Path | None = None
        try:
            validated = rollout.load_manifest(manifest)
        except rollout.RolloutError as exc:
            raise PinUpdateError(str(exc)) from exc
        spec = validated["repositories"].get(repository)
        raw_paths = spec["workflow_paths"] if spec else None
    else:
        raise PinUpdateError("consumer manifest must use schema 1 or schema 2")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise PinUpdateError(f"repository is not allowlisted: {repository}")

    paths: list[str] = []
    for raw in raw_paths:
        if not isinstance(raw, str):
            raise PinUpdateError(f"{repository}: non-string path in manifest")
        pure = PurePosixPath(raw)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or not raw.startswith(".github/workflows/")
            or pure.suffix not in {".yml", ".yaml"}
        ):
            raise PinUpdateError(f"{repository}: unsafe workflow path {raw!r}")
        normalized = pure.as_posix()
        if normalized in paths:
            raise PinUpdateError(f"{repository}: duplicate workflow path {raw!r}")
        paths.append(normalized)
    return paths


def semantic_kin_actions_uses(path: Path, relative: str) -> list[tuple[int, str]]:
    """Return every semantic kin-actions reusable-workflow use and any ref."""

    try:
        uses = _semantic.semantic_uses(path)
    except _semantic.WorkflowPinError as exc:
        raise PinUpdateError(f"{relative}: {exc}") from exc
    return [
        (line, value)
        for line, value in uses
        if KIN_ACTIONS_USE_RE.fullmatch(value)
    ]


def semantic_pins(path: Path, relative: str) -> list[tuple[int, str]]:
    """Return every stable kin-actions reusable-workflow use in one file.

    The uses surface comes from the semantic YAML parser rather than a raw
    text scan, so a pin written as a quoted, folded, or aliased scalar is
    still seen. A kin-actions workflow reached through any ref other than one
    stable release tag is refused instead of being left out of the wave.
    """

    found: list[tuple[int, str]] = []
    for line, value in semantic_kin_actions_uses(path, relative):
        match = KIN_ACTIONS_USE_RE.fullmatch(value)
        assert match is not None
        if not STABLE_TAG_RE.fullmatch(match.group("ref")):
            raise PinUpdateError(
                f"{relative}: line {line}: kin-actions workflow is not pinned "
                f"to one stable release tag: {value!r}"
            )
        found.append((line, value))
    return found


def discover_pin_paths(root: Path, *, require_stable: bool = True) -> list[str]:
    """Find every live kin-actions reusable-workflow pin in a checkout."""

    root = root.resolve()
    workflow_root = root / ".github" / "workflows"
    if not workflow_root.exists():
        return []
    if workflow_root.is_symlink() or not workflow_root.is_dir():
        raise PinUpdateError(
            ".github/workflows is not a regular directory"
        )

    discovered: list[str] = []
    for directory, directories, filenames in os.walk(
        workflow_root, followlinks=False
    ):
        directory_path = Path(directory)
        for name in directories:
            candidate = directory_path / name
            if candidate.is_symlink():
                raise PinUpdateError(
                    "workflow tree contains a symlink directory: "
                    + str(candidate.relative_to(root))
                )
        for name in filenames:
            path = directory_path / name
            if path.suffix not in {".yml", ".yaml"}:
                continue
            if path.is_symlink() or not path.is_file():
                raise PinUpdateError(
                    "workflow tree contains a non-regular file: "
                    + str(path.relative_to(root))
                )
            relative = path.relative_to(root).as_posix()
            semantic = (
                semantic_pins(path, relative)
                if require_stable
                else semantic_kin_actions_uses(path, relative)
            )
            if semantic:
                discovered.append(relative)
    return sorted(discovered)


def plan_updates(
    *,
    root: Path,
    paths: list[str],
    target_version: str,
) -> dict[Path, bytes]:
    root = root.resolve()
    target = parse_version(target_version)
    planned: dict[Path, bytes] = {}
    for relative in paths:
        path = (root / relative).absolute()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise PinUpdateError(f"allowlisted path escapes checkout: {relative}") from exc
        if path.is_symlink() or not path.is_file():
            raise PinUpdateError(f"allowlisted path is not a regular file: {relative}")
        text = path.read_text(encoding="utf-8")
        semantic = semantic_pins(path, relative)
        if not semantic:
            raise PinUpdateError(
                f"allowlisted workflow has no exact kin-actions pin: {relative}"
            )
        matches = list(PIN_RE.finditer(text))
        semantic_lines = [line for line, _value in semantic]
        raw_lines = [text.count("\n", 0, match.start()) + 1 for match in matches]
        if len(semantic_lines) != len(set(semantic_lines)):
            raise PinUpdateError(
                f"{relative}: multiple semantic kin-actions pins share one line; "
                "canonical one-line uses entries are required"
            )
        if raw_lines != semantic_lines:
            raise PinUpdateError(
                f"{relative}: {len(semantic)} semantic kin-actions pin(s) but "
                f"{len(matches)} canonical rewritable pin(s) at different lines; "
                "refusing a partial rewrite; comments, folded scalars, and noncanonical pins are forbidden"
            )
        for match, (line, semantic_value) in zip(matches, semantic, strict=True):
            line_start = text.rfind("\n", 0, match.start()) + 1
            before = text[line_start : match.start()]
            if not re.fullmatch(r"[ \t]*(?:-[ \t]+)?", before):
                raise PinUpdateError(
                    f"{relative}: line {line}: kin-actions pin is not one canonical uses entry"
                )
            textual_value = match.group("prefix").split("uses:", 1)[1].strip()
            textual_value += match.group("version")
            if textual_value != semantic_value:
                raise PinUpdateError(
                    f"{relative}: line {line}: textual and semantic pin authority differ"
                )
            current_raw = match.group("version")
            current = parse_version(current_raw)
            if current > target:
                raise PinUpdateError(
                    f"refusing to downgrade {relative} from v{current_raw} "
                    f"to v{target_version}"
                )
        updated = PIN_RE.sub(
            lambda match: match.group("prefix") + target_version,
            text,
        )
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=path.suffix,
                delete=False,
            ) as stream:
                stream.write(updated)
                planned_path = Path(stream.name)
            planned_semantic = semantic_pins(planned_path, relative)
        finally:
            if planned_path is not None:
                planned_path.unlink(missing_ok=True)
        expected_value_suffix = f"@v{target_version}"
        if (
            [line for line, _value in planned_semantic] != semantic_lines
            or any(
                not value.endswith(expected_value_suffix)
                for _line, value in planned_semantic
            )
        ):
            raise PinUpdateError(
                f"{relative}: planned bytes do not semantically pin every use to v{target_version}"
            )
        if updated != text:
            planned[path] = updated.encode("utf-8")
    return planned


def update_pins(
    *,
    root: Path,
    manifest: Path,
    repository: str,
    target_version: str,
) -> dict[str, object]:
    parse_version(target_version)
    paths = load_consumer_paths(manifest, repository)
    discovered = discover_pin_paths(root)
    expected = sorted(paths)
    if discovered != expected:
        missing = sorted(set(discovered) - set(expected))
        stale = sorted(set(expected) - set(discovered))
        details = []
        if missing:
            details.append("unmanifested live pins: " + ", ".join(missing))
        if stale:
            details.append("manifest paths without live pins: " + ", ".join(stale))
        raise PinUpdateError(
            f"{repository}: consumer manifest is not exact; "
            + "; ".join(details)
        )
    planned = plan_updates(
        root=root,
        paths=paths,
        target_version=target_version,
    )
    # Every path and every candidate version has been validated before the
    # first write, so a mixed newer/older checkout cannot be partially updated.
    for path, data in planned.items():
        path.write_bytes(data)
    changed = sorted(str(path.relative_to(root.resolve())) for path in planned)
    return {
        "repository": repository,
        "version": target_version,
        "tag": f"v{target_version}",
        "changed": bool(changed),
        "changed_paths": changed,
        "allowed_paths": paths,
    }


def _write_outputs(result: dict[str, object]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as stream:
        stream.write(f"changed={'true' if result['changed'] else 'false'}\n")
        stream.write(
            "changed_paths="
            + " ".join(str(item) for item in result["changed_paths"])
            + "\n"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="update allowlisted immutable kin-actions workflow pins"
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        result = update_pins(
            root=args.root,
            manifest=args.manifest.resolve(),
            repository=args.repository,
            target_version=args.version,
        )
    except PinUpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _write_outputs(result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
