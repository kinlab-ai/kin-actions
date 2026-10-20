#!/usr/bin/env python3
"""Strict Kin sparse-registry index and SemVer authority.

Only an HTTP 404 represents an unpublished crate. Every successful response
must contain at least one valid Cargo index row; transport, decoding, JSON, and
schema failures are authority failures rather than an empty registry.

An HTTP 5xx is retried with backoff. If the registry still answers 5xx, the
fetch raises RegistryUnavailableError (a RegistryIndexError) so a scheduled
caller can report an outage instead of a broken run. It is never read as
"unpublished".

Every request names this client in its User-Agent. The edge in front of the
hosted registry refuses urllib's default ``Python-urllib/x.y`` agent with
Cloudflare error 1010, which arrives as HTTP 403 and would otherwise fail every
caller even while the registry is healthy.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


# SemVer numeric identifiers are ASCII by grammar. Python's generic digit
# character class is broader, so every numeric branch stays explicit.
_CORE_IDENTIFIER = r"(?:0|[1-9][0-9]*)"
_PRERELEASE_IDENTIFIER = (
    r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
)
_BUILD_IDENTIFIER = r"[0-9A-Za-z-]+"
_SEMVER = re.compile(
    rf"^(?P<major>{_CORE_IDENTIFIER})\."
    rf"(?P<minor>{_CORE_IDENTIFIER})\."
    rf"(?P<patch>{_CORE_IDENTIFIER})"
    rf"(?:-(?P<prerelease>{_PRERELEASE_IDENTIFIER}"
    rf"(?:\.{_PRERELEASE_IDENTIFIER})*))?"
    rf"(?:\+(?P<build>{_BUILD_IDENTIFIER}"
    rf"(?:\.{_BUILD_IDENTIFIER})*))?$"
)
_CHECKSUM = re.compile(r"^[0-9a-fA-F]{64}$")


class RegistryIndexError(RuntimeError):
    """Sparse-index authority could not be established."""


class RegistryUnavailableError(RegistryIndexError):
    """The registry kept answering HTTP 5xx after every retry."""


# Delays between attempts on an HTTP 5xx: four attempts, about 14s in all.
RETRY_DELAYS_SECONDS: tuple[float, ...] = (2, 4, 8)

REPOSITORY_URL = "https://github.com/kinlab-ai/kin-actions"


def _helper_version() -> str:
    """Return the kin-actions VERSION these helper bytes were checked out with."""

    try:
        version = (
            Path(__file__).resolve().parent.parent / "VERSION"
        ).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return "unknown"
    return version if _SEMVER.fullmatch(version) else "unknown"


USER_AGENT = f"kin-actions/{_helper_version()} (+{REPOSITORY_URL})"


@dataclass(frozen=True)
class RegistryVersion:
    """One immutable version record from a Cargo sparse index."""

    name: str
    version: str
    yanked: bool
    checksum: str


def parse_version(version: str) -> tuple[object, ...]:
    """Return a full SemVer precedence key, ignoring build metadata."""

    match = _SEMVER.fullmatch(version)
    if match is None:
        raise ValueError(f"invalid SemVer: {version!r}")
    prerelease = match.group("prerelease")
    identifiers: tuple[tuple[int, object], ...] = ()
    if prerelease is not None:
        identifiers = tuple(
            (0, int(identifier))
            if identifier.isdigit()
            else (1, identifier)
            for identifier in prerelease.split(".")
        )
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        prerelease is None,
        identifiers,
    )


def sparse_index_path(name: str) -> str:
    """Return the Cargo sparse-index path for one crate name."""

    name = name.lower()
    if len(name) == 1:
        return f"1/{name}"
    if len(name) == 2:
        return f"2/{name}"
    if len(name) == 3:
        return f"3/{name[0]}/{name}"
    return f"{name[:2]}/{name[2:4]}/{name}"


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def parse_index(
    body: bytes,
    *,
    crate_name: str,
    source: str,
) -> tuple[RegistryVersion, ...]:
    """Parse and validate every row in a successful sparse-index response."""

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegistryIndexError(
            f"could not decode registry index {source} as UTF-8: {exc}"
        ) from exc

    records: list[RegistryVersion] = []
    version_rows: dict[str, int] = {}
    rows_seen = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        rows_seen += 1
        try:
            obj = json.loads(
                line,
                parse_constant=_reject_nonstandard_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                f"invalid JSON ({exc})"
            ) from exc
        if not isinstance(obj, dict):
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                "expected an object"
            )

        name = obj.get("name")
        version = obj.get("vers")
        yanked = obj.get("yanked")
        checksum = obj.get("cksum")
        dependencies = obj.get("deps")
        features = obj.get("features")

        if not isinstance(name, str) or not name:
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                "missing string 'name'"
            )
        if name != crate_name:
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                f"crate name {name!r} does not match {crate_name!r}"
            )
        if not isinstance(version, str) or not version:
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                "missing string 'vers'"
            )
        try:
            parse_version(version)
        except ValueError as exc:
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: {exc}"
            ) from exc
        if version in version_rows:
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                f"duplicate immutable version {crate_name}@{version}; "
                f"first seen on row {version_rows[version]}"
            )
        version_rows[version] = line_number
        if not isinstance(yanked, bool):
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                "missing boolean 'yanked'"
            )
        if not isinstance(checksum, str) or not _CHECKSUM.fullmatch(checksum):
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                "missing 64-hex 'cksum'"
            )
        if not isinstance(dependencies, list):
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                "missing list 'deps'"
            )
        if not isinstance(features, dict):
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                "missing object 'features'"
            )

        records.append(
            RegistryVersion(
                name=name,
                version=version,
                yanked=yanked,
                checksum=checksum.lower(),
            )
        )

    if rows_seen == 0:
        raise RegistryIndexError(
            f"malformed registry index at {source}: empty successful response; "
            "an unpublished crate must return HTTP 404"
        )
    return tuple(records)


def fetch_index(
    registry_url: str,
    crate_name: str,
    *,
    timeout: float = 10,
    opener: Callable[..., object] | None = None,
    retry_delays: tuple[float, ...] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[RegistryVersion, ...] | None:
    """Fetch one strict sparse index, returning ``None`` only for HTTP 404.

    An HTTP 5xx is retried after each delay in ``retry_delays``. If every
    attempt answers 5xx, RegistryUnavailableError is raised.
    """

    if opener is None:
        opener = urllib.request.urlopen
    if retry_delays is None:
        retry_delays = RETRY_DELAYS_SECONDS
    if sleep is None:
        sleep = time.sleep
    url = (
        f"{registry_url.rstrip('/')}/registry/cargo/"
        f"{sparse_index_path(crate_name)}"
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    attempts = len(retry_delays) + 1
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                with opener(request, timeout=timeout) as response:
                    body = response.read()
                break
            except urllib.error.HTTPError as exc:
                if 500 <= exc.code <= 599 and attempt < attempts:
                    sleep(retry_delays[attempt - 1])
                    continue
                raise
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if 500 <= exc.code <= 599:
            raise RegistryUnavailableError(
                f"could not read registry index {url}: HTTP {exc.code} "
                f"after {attempts} attempts"
            ) from exc
        raise RegistryIndexError(
            f"could not read registry index {url}: HTTP {exc.code}"
        ) from exc
    except Exception as exc:
        raise RegistryIndexError(
            f"could not read registry index {url}: {exc}"
        ) from exc
    if not isinstance(body, bytes):
        raise RegistryIndexError(
            f"could not read registry index {url}: response body was not bytes"
        )
    return parse_index(body, crate_name=crate_name, source=url)
