#!/usr/bin/env python3
"""Prepare kin-actions' own generated VERSION and documentation bytes."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
PIN_RE = re.compile(
    r"(?P<prefix>(?:kinlab-ai|firelock-ai)/kin-actions/\.github/workflows/"
    r"[A-Za-z0-9_.-]+\.ya?ml@v)"
    r"(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*))"
)


class SelfReleaseError(RuntimeError):
    """A self-release invariant failed."""


def parse_version(raw: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(raw.strip())
    if not match:
        raise SelfReleaseError(
            f"automatic releases require stable numeric SemVer, got {raw!r}"
        )
    return tuple(int(part) for part in match.groups())


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def successor(
    version: tuple[int, int, int], intent: str
) -> tuple[int, int, int]:
    major, minor, patch = version
    if intent == "patch":
        return major, minor, patch + 1
    if intent == "minor":
        return major, minor + 1, 0
    if intent == "major":
        return major + 1, 0, 0
    raise SelfReleaseError(f"invalid release intent: {intent!r}")


def choose_target(base_raw: str, current_raw: str, intent: str) -> str:
    base = parse_version(base_raw)
    current = parse_version(current_raw)
    requested = successor(base, intent)
    valid = {successor(base, level) for level in ("patch", "minor", "major")}
    if current == base:
        chosen = requested
    elif current not in valid:
        raise SelfReleaseError(
            f"current VERSION {current_raw} is neither base {base_raw} nor an "
            "automatic successor"
        )
    else:
        chosen = max(current, requested)
    return format_version(chosen)


def _regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise SelfReleaseError(f"generated input is not a regular file: {path}")


def _validate_relative(relative: str) -> str:
    pure = PurePosixPath(relative)
    if (
        not relative
        or pure.is_absolute()
        or pure.as_posix() != relative
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise SelfReleaseError(
            "generated path must be normalized and repository-relative: "
            f"{relative!r}"
        )
    return relative


def _git(
    root: Path,
    args: list[str],
    *,
    text: bool = False,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "--no-replace-objects", *args],
        cwd=root,
        text=text,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (
            (result.stderr or result.stdout).strip()
            if text
            else (result.stderr or result.stdout).decode(
                "utf-8", errors="replace"
            ).strip()
        )
        raise SelfReleaseError(
            f"git {' '.join(args)} failed: {detail or result.returncode}"
        )
    return result


def _blob_at_ref(root: Path, ref: str, relative: str) -> bytes:
    relative = _validate_relative(relative)
    return _git(root, ["show", f"{ref}:{relative}"]).stdout


def _tree_mode_at_ref(root: Path, ref: str, relative: str) -> str:
    relative = _validate_relative(relative)
    result = _git(
        root,
        [
            "ls-tree",
            "-z",
            "--full-tree",
            ref,
            "--",
            f":(literal){relative}",
        ],
    )
    records = [record for record in result.stdout.split(b"\0") if record]
    if len(records) != 1:
        raise SelfReleaseError(
            f"expected one tree entry for {relative} at {ref}"
        )
    try:
        header, raw_path = records[0].split(b"\t", 1)
        mode, object_type, _oid = header.decode("ascii").split(" ", 2)
        path = raw_path.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise SelfReleaseError(
            f"malformed tree entry for {relative} at {ref}"
        ) from exc
    if path != relative or object_type != "blob" or mode == "120000":
        raise SelfReleaseError(
            f"unsafe generated tree entry for {relative} at {ref}"
        )
    return mode


def _intent_for_successor(base_raw: str, target_raw: str) -> str:
    base = parse_version(base_raw)
    target = parse_version(target_raw)
    matches = [
        intent
        for intent in ("patch", "minor", "major")
        if successor(base, intent) == target
    ]
    if len(matches) != 1:
        raise SelfReleaseError(
            f"generated target {target_raw} is not one automatic successor "
            f"of {base_raw}"
        )
    return matches[0]


def prepare_self_release(
    *,
    root: Path,
    base_version: str,
    intent: str,
    version_file: str = "VERSION",
    docs: tuple[str, ...] = ("README.md", "CONTRIBUTING.md"),
) -> dict[str, object]:
    root = root.resolve()
    version_path = (root / version_file).absolute()
    try:
        version_path.relative_to(root)
    except ValueError as exc:
        raise SelfReleaseError("VERSION path escapes repository root") from exc
    _regular_file(version_path)
    current_raw = version_path.read_text(encoding="utf-8").strip()
    target = choose_target(base_version, current_raw, intent)
    target_tuple = parse_version(target)

    paths = [version_path]
    for relative in docs:
        path = (root / relative).absolute()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise SelfReleaseError(f"doc path escapes repository root: {relative}") from exc
        _regular_file(path)
        paths.append(path)

    originals = {path: path.read_bytes() for path in paths}
    planned: dict[Path, bytes] = {version_path: (target + "\n").encode("utf-8")}
    for path in paths[1:]:
        text = path.read_text(encoding="utf-8")
        for match in PIN_RE.finditer(text):
            found = match.group("version")
            if parse_version(found) > target_tuple:
                raise SelfReleaseError(
                    f"refusing to downgrade {path.relative_to(root)} from "
                    f"v{found} to v{target}"
                )
        updated = PIN_RE.sub(lambda match: match.group("prefix") + target, text)
        planned[path] = updated.encode("utf-8")

    for path, data in planned.items():
        path.write_bytes(data)

    changed = sorted(
        str(path.relative_to(root))
        for path, original in originals.items()
        if path.read_bytes() != original
    )
    chosen_tuple = parse_version(target)
    base_tuple = parse_version(base_version)
    chosen_intent = next(
        level
        for level in ("major", "minor", "patch")
        if successor(base_tuple, level) == chosen_tuple
    )
    return {
        "base_version": base_version,
        "previous_generated_version": current_raw,
        "target_version": target,
        "tag": f"v{target}",
        "intent": chosen_intent,
        "generated_paths": changed,
        "allowed_paths": sorted(str(path.relative_to(root)) for path in paths),
    }


def verify_self_release(
    *,
    root: Path,
    base_ref: str,
    base_version: str,
    target_version: str,
    generated_paths: list[str],
    version_file: str = "VERSION",
    docs: tuple[str, ...] = ("README.md", "CONTRIBUTING.md"),
) -> dict[str, object]:
    """Reconstruct and compare every self-release-owned byte and tree mode."""

    root = root.resolve()
    expected_paths = sorted(
        {
            _validate_relative(version_file),
            *(_validate_relative(path) for path in docs),
        }
    )
    generated = sorted(_validate_relative(path) for path in generated_paths)
    if generated != expected_paths:
        raise SelfReleaseError(
            "generated verification path set must equal the exact self-release "
            f"allowlist: expected {expected_paths}, got {generated}"
        )
    intent = _intent_for_successor(base_version, target_version)

    with tempfile.TemporaryDirectory(
        prefix="kin-self-release-verify-"
    ) as directory:
        fixture = Path(directory)
        for relative in expected_paths:
            destination = fixture / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_blob_at_ref(root, base_ref, relative))

        result = prepare_self_release(
            root=fixture,
            base_version=base_version,
            intent=intent,
            version_file=version_file,
            docs=docs,
        )
        if result["target_version"] != target_version:
            raise SelfReleaseError(
                "deterministic generator produced "
                f"{result['target_version']}, expected {target_version}"
            )

        for relative in expected_paths:
            expected = (fixture / relative).read_bytes()
            actual = _blob_at_ref(root, "HEAD", relative)
            if actual != expected:
                raise SelfReleaseError(
                    "generated path does not match deterministic bytes: "
                    f"{relative}"
                )
            base_mode = _tree_mode_at_ref(root, base_ref, relative)
            head_mode = _tree_mode_at_ref(root, "HEAD", relative)
            if head_mode != base_mode:
                raise SelfReleaseError(
                    f"generated path mode changed from {base_mode} to "
                    f"{head_mode}: {relative}"
                )

    return {
        "base_ref": base_ref,
        "base_version": base_version,
        "target_version": target_version,
        "intent": intent,
        "verified_paths": expected_paths,
    }


def _write_outputs(result: dict[str, object]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as stream:
        for key in (
            "base_version",
            "previous_generated_version",
            "target_version",
            "tag",
            "intent",
        ):
            stream.write(f"{key}={result[key]}\n")
        stream.write(
            "generated_paths="
            + " ".join(str(path) for path in result["generated_paths"])
            + "\n"
        )
        stream.write(
            "allowed_paths="
            + " ".join(str(path) for path in result["allowed_paths"])
            + "\n"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="prepare kin-actions' own automatic release bytes"
    )
    parser.add_argument("--base-version", required=True)
    parser.add_argument("--intent", choices=("patch", "minor", "major"), default="patch")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--version-file", default="VERSION")
    parser.add_argument("--doc", action="append", default=[])
    parser.add_argument(
        "--verify-ref",
        help="rebuild exact generated bytes from this immutable Git base",
    )
    parser.add_argument("--target-version")
    parser.add_argument("--generated-path", action="append", default=[])
    args = parser.parse_args(argv)
    docs = tuple(args.doc) if args.doc else ("README.md", "CONTRIBUTING.md")
    try:
        if args.verify_ref:
            if not args.target_version:
                parser.error("--target-version is required with --verify-ref")
            result = verify_self_release(
                root=args.root,
                base_ref=args.verify_ref,
                base_version=args.base_version,
                target_version=args.target_version,
                generated_paths=args.generated_path,
                version_file=args.version_file,
                docs=docs,
            )
        else:
            result = prepare_self_release(
                root=args.root,
                base_version=args.base_version,
                intent=args.intent,
                version_file=args.version_file,
                docs=docs,
            )
    except SelfReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not args.verify_ref:
        _write_outputs(result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
