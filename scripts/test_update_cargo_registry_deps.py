#!/usr/bin/env python3
"""Tests for the transactional Kin Cargo dependency-wave updater."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


def _load(name, filename):
    path = Path(__file__).resolve().parent / filename
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ucd = _load("update_cargo_registry_deps", "update-cargo-registry-deps.py")
CHECKSUM = "0" * 64


def registry_row(
    crate="kin-model",
    version="0.7.0",
    *,
    yanked=False,
    omit=(),
):
    row = {
        "name": crate,
        "vers": version,
        "yanked": yanked,
        "cksum": CHECKSUM,
        "deps": [],
        "features": {},
    }
    for key in omit:
        row.pop(key)
    return (json.dumps(row, separators=(",", ":")) + "\n").encode()


def registry_record(
    version,
    *,
    crate="kin-model",
    yanked=False,
    checksum=CHECKSUM,
):
    return ucd.registry_index.RegistryVersion(
        name=crate,
        version=version,
        yanked=yanked,
        checksum=checksum,
    )


class _RegistryServer:
    def __init__(self, bodies):
        self.bodies = dict(bodies)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib callback
                crate = self.path.rsplit("/", 1)[-1]
                body = outer.bodies.get(crate)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    @property
    def url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

KIN_MANIFEST = """\
[dependencies]
kin-db = { version = "0.2.24", registry = "kin" }
serde = { version = "1", features = ["derive"] }
"""


class TemporaryManifestTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class RequirementUpdates(unittest.TestCase):
    def test_semver_precedence_prefers_stable_and_orders_prereleases(self):
        versions = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        ]
        self.assertEqual(sorted(versions, key=ucd.parse_version), versions)
        self.assertEqual(
            max(
                ["1.0.0-alpha+build.9", "1.0.0+build.1"],
                key=ucd.parse_version,
            ),
            "1.0.0+build.1",
        )

    def test_preserves_supported_operators_and_spacing(self):
        for requirement, expected in (
            ("0.6.4", "0.7.0"),
            ("=0.6.4", "=0.7.0"),
            ("^0.6.4", "^0.7.0"),
            ("~0.6.4", "~0.7.0"),
            ("= 0.6.4", "= 0.7.0"),
        ):
            with self.subTest(requirement=requirement):
                self.assertEqual(
                    ucd.update_requirement(requirement, "0.7.0"), expected
                )

    def test_preserves_pre_release_and_build_target(self):
        self.assertEqual(
            ucd.update_requirement(
                "=0.6.4-alpha.1+build.7", "0.7.0-rc.1+release.2"
            ),
            "=0.7.0-rc.1+release.2",
        )

    def test_stale_event_cannot_downgrade_a_current_requirement(self):
        for requirement in ("0.8.0", "=0.8.0", "^0.8", "~0.8.1"):
            with self.subTest(requirement=requirement):
                self.assertEqual(
                    ucd.update_requirement(requirement, "0.7.0"), requirement
                )

    def test_equal_precedence_partial_requirement_is_idempotent(self):
        for requirement in (
            "0.8",
            "=0.8",
            "^0.8",
            "~0.8",
            "1",
            "^1",
            "1.0",
            "=1.0",
            "^1.0",
            "~1.0",
        ):
            target = (
                "1.0.0"
                if requirement.lstrip("=^~ ") in {"1", "1.0"}
                else "0.8.0"
            )
            with self.subTest(requirement=requirement, target=target):
                self.assertEqual(
                    ucd.update_requirement(requirement, target), requirement
                )

    def test_compound_and_malformed_requirements_fail_loud(self):
        for requirement in (
            ">=0.6, <0.7",
            "0.6 || 0.7",
            "*",
            ">0.6",
            "0.6.*",
            "01.6.4",
            "0.6.4-01",
            "0.6.4-alpha..1",
            "0.6-alpha",
            "0.6.4+",
            "",
        ):
            with self.subTest(requirement=requirement):
                with self.assertRaisesRegex(
                    ucd.UpdateError, "unsupported Cargo requirement"
                ):
                    ucd.update_requirement(requirement, "0.7.0")

    def test_malformed_target_version_fails_loud(self):
        for version in (
            "v0.7.0",
            ">=0.7",
            "",
            "0.7.*",
            "0.7",
            "01.7.0",
            "0.7.0-01",
            "0.7.0-",
            "0.7.0+",
        ):
            with self.subTest(version=version):
                with self.assertRaisesRegex(
                    ucd.UpdateError, "unsupported target version"
                ):
                    ucd.update_requirement("=0.6.4", version)


class RegistryVersions(unittest.TestCase):
    def test_latest_version_uses_semver_and_ignores_yanked_entries(self):
        body = b"".join(
            (
                registry_row(version="1.0.0-alpha.1"),
                registry_row(version="1.0.0"),
                registry_row(version="2.0.0", yanked=True),
            )
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        with mock.patch.object(
            ucd.registry_index.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            self.assertEqual(
                ucd.latest_version("https://kinlab.ai", "kin-model"), "1.0.0"
            )
        urlopen.assert_called_once_with(
            "https://kinlab.ai/registry/cargo/ki/n-/kin-model", timeout=10
        )

    def test_invalid_registry_version_fails_loud(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = registry_row(
            version="01.0.0"
        )
        with mock.patch.object(
            ucd.registry_index.urllib.request, "urlopen", return_value=response
        ):
            with self.assertRaisesRegex(
                ucd.UpdateError, "invalid SemVer"
            ):
                ucd.latest_version("https://kinlab.ai", "kin-model")

    def test_empty_successful_registry_response_fails_closed(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"\n"
        with mock.patch.object(
            ucd.registry_index.urllib.request, "urlopen", return_value=response
        ):
            with self.assertRaisesRegex(ucd.UpdateError, "empty successful response"):
                ucd.latest_version("https://kinlab.ai", "kin-model")

    def test_wrong_crate_registry_row_fails_closed(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = registry_row(crate="kin-db")
        with mock.patch.object(
            ucd.registry_index.urllib.request, "urlopen", return_value=response
        ):
            with self.assertRaisesRegex(ucd.UpdateError, "does not match"):
                ucd.latest_version("https://kinlab.ai", "kin-model")

    def test_stale_event_coalesces_to_registry_latest(self):
        with mock.patch.object(
            ucd,
            "registry_records",
            return_value=(registry_record("0.8.0"),),
        ):
            self.assertEqual(
                ucd._resolve_versions(
                    ["kin-model"], "0.7.0", "https://kinlab.ai"
                ),
                {"kin-model": "0.8.0"},
            )

    def test_event_ahead_of_visible_index_is_preserved(self):
        with mock.patch.object(
            ucd,
            "registry_records",
            return_value=(registry_record("0.7.0"),),
        ):
            self.assertEqual(
                ucd._resolve_versions(
                    ["kin-model"], "0.8.0", "https://kinlab.ai"
                ),
                {"kin-model": "0.8.0"},
            )

    def test_event_version_applies_only_to_event_crate_during_full_refresh(self):
        records = {
            "kin-model": (registry_record("0.7.0"),),
            "kin-db": (registry_record("0.6.6", crate="kin-db"),),
        }
        with mock.patch.object(
            ucd,
            "registry_records",
            side_effect=lambda _url, crate: records[crate],
        ):
            self.assertEqual(
                ucd._resolve_versions(
                    ["kin-model", "kin-db"],
                    "0.8.0",
                    "https://kinlab.ai",
                    requested_crate="kin-model",
                ),
                {"kin-model": "0.8.0", "kin-db": "0.6.6"},
            )

    def test_replayed_exact_yanked_event_is_not_a_candidate(self):
        with mock.patch.object(
            ucd,
            "registry_records",
            return_value=(
                registry_record("1.2.2"),
                registry_record("1.2.3", yanked=True),
            ),
        ):
            self.assertEqual(
                ucd._resolve_versions(
                    ["kin-model"],
                    "1.2.3",
                    "https://kinlab.ai",
                    requested_crate="kin-model",
                ),
                {"kin-model": "1.2.2"},
            )

    def test_replayed_yanked_event_with_no_installable_version_is_skipped(self):
        with mock.patch.object(
            ucd,
            "registry_records",
            return_value=(registry_record("1.2.3", yanked=True),),
        ):
            self.assertEqual(
                ucd._resolve_versions(
                    ["kin-model"],
                    "1.2.3",
                    "https://kinlab.ai",
                    requested_crate="kin-model",
                ),
                {},
            )


class ManifestRewriting(TemporaryManifestTest):
    def test_real_run_rewrites_kin_pin(self):
        path = self.write("Cargo.toml", KIN_MANIFEST)
        self.assertTrue(ucd.update_manifest(str(path), "kin-db", "0.2.25"))
        self.assertIn(
            'kin-db = { version = "0.2.25", registry = "kin" }',
            path.read_text(encoding="utf-8"),
        )

    def test_dry_run_reports_without_writing(self):
        path = self.write("Cargo.toml", KIN_MANIFEST)
        self.assertTrue(
            ucd.update_manifest(str(path), "kin-db", "0.2.25", dry_run=True)
        )
        self.assertEqual(path.read_text(encoding="utf-8"), KIN_MANIFEST)

    def test_non_kin_dependency_is_untouched(self):
        path = self.write("Cargo.toml", KIN_MANIFEST)
        self.assertFalse(ucd.update_manifest(str(path), "serde", "2"))
        self.assertEqual(path.read_text(encoding="utf-8"), KIN_MANIFEST)

    def test_direct_key_with_different_package_is_untouched(self):
        manifest = (
            "[dependencies]\n"
            "kin-model = { package = 'different-package', "
            "version = '=0.6.4', registry = 'kin' }\n"
        )
        path = self.write("Cargo.toml", manifest)
        self.assertFalse(ucd.update_manifest(str(path), "kin-model", "0.7.0"))
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)

    def test_already_current_pin_is_noop(self):
        path = self.write("Cargo.toml", KIN_MANIFEST)
        self.assertFalse(ucd.update_manifest(str(path), "kin-db", "0.2.24"))
        self.assertEqual(path.read_text(encoding="utf-8"), KIN_MANIFEST)

    def test_compact_single_quoted_inline_table_is_supported(self):
        path = self.write(
            "Cargo.toml",
            "[dependencies]\n"
            "model={package='kin-model',version='=0.6.4',registry='kin'}\n",
        )
        self.assertTrue(ucd.update_manifest(str(path), "kin-model", "0.7.0"))
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "[dependencies]\n"
            "model={package='kin-model',version='=0.7.0',registry='kin'}\n",
        )

    def test_inline_alias_preserves_operator(self):
        path = self.write(
            "Cargo.toml",
            "[dependencies]\n"
            'model = { package = "kin-model", version = "~0.6.4", '
            'registry = "kin" }\n',
        )
        self.assertTrue(ucd.update_manifest(str(path), "kin-model", "0.7.0"))
        self.assertIn(
            'version = "~0.7.0"', path.read_text(encoding="utf-8")
        )

    def test_inline_alias_field_order_is_irrelevant(self):
        path = self.write(
            "Cargo.toml",
            "[dependencies]\n"
            "model = { registry = 'kin', version = '^0.6.4', "
            "features = ['serde'], package = 'kin-model' }\n",
        )
        self.assertTrue(ucd.update_manifest(str(path), "kin-model", "0.7.0"))
        self.assertIn(
            "registry = 'kin', version = '^0.7.0'",
            path.read_text(encoding="utf-8"),
        )

    def test_inline_version_key_is_selected_at_top_level(self):
        path = self.write(
            "Cargo.toml",
            "[dependencies]\n"
            "model = { package = 'kin-model', metadata = { version = '9.9.9' }, "
            "version = '=0.6.4', registry = 'kin' }\n",
        )
        self.assertTrue(ucd.update_manifest(str(path), "kin-model", "0.7.0"))
        output = path.read_text(encoding="utf-8")
        self.assertIn("metadata = { version = '9.9.9' }", output)
        self.assertIn("version = '=0.7.0', registry", output)

    def test_dependency_table_form_and_alias_are_supported(self):
        path = self.write(
            "Cargo.toml",
            "[workspace.dependencies.model]\n"
            "package = 'kin-model'\n"
            "version = '=0.6.4' # exact by policy\n"
            "registry = 'kin'\n",
        )
        self.assertTrue(ucd.update_manifest(str(path), "kin-model", "0.7.0"))
        self.assertIn(
            "version = '=0.7.0' # exact by policy",
            path.read_text(encoding="utf-8"),
        )

    def test_target_dependency_table_is_supported(self):
        path = self.write(
            "Cargo.toml",
            "[target.'cfg(unix)'.dependencies.kin-db]\n"
            'version="^0.6.4"\n'
            'registry="kin"\n',
        )
        self.assertTrue(ucd.update_manifest(str(path), "kin-db", "0.7.0"))
        self.assertIn('version="^0.7.0"', path.read_text(encoding="utf-8"))

    def test_root_dotted_dependency_key_is_supported(self):
        path = self.write(
            "Cargo.toml",
            "dependencies.kin-model={version='=0.6.4',registry='kin'}\n",
        )
        self.assertTrue(ucd.update_manifest(str(path), "kin-model", "0.7.0"))
        self.assertIn("version='=0.7.0'", path.read_text(encoding="utf-8"))

    def test_compound_requirement_fails_without_writing(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = ">=0.6, <0.7", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        with self.assertRaisesRegex(
            ucd.UpdateError, "unsupported Cargo requirement"
        ):
            ucd.update_manifest(str(path), "kin-model", "0.7.0")
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)

    def test_escaped_version_literal_fails_without_writing(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "\\u003d0.6.4", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        with self.assertRaisesRegex(ucd.UpdateError, "escaped or ambiguous"):
            ucd.update_manifest(str(path), "kin-model", "0.7.0")
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)

    def test_multiline_version_literal_fails_without_writing(self):
        manifest = (
            "[dependencies.kin-model]\n"
            'version = """=0.6.4"""\n'
            'registry = "kin"\n'
        )
        path = self.write("Cargo.toml", manifest)
        with self.assertRaisesRegex(
            ucd.UpdateError, "physical multiline TOML strings"
        ):
            ucd.update_manifest(str(path), "kin-model", "0.7.0")
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)

    def test_registry_dependency_without_string_version_fails(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = 640, registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        with self.assertRaisesRegex(ucd.UpdateError, "has no string version"):
            ucd.update_manifest(str(path), "kin-model", "0.7.0")
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)

    def test_malformed_toml_fails_without_writing(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" \n'
        )
        path = self.write("Cargo.toml", manifest)
        with self.assertRaisesRegex(ucd.UpdateError, "malformed TOML"):
            ucd.update_manifest(str(path), "kin-model", "0.7.0")
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)

    def test_physical_multiline_string_fails_loud_without_writing(self):
        manifest = (
            "[dependencies.kin-model]\n"
            'note = """\n'
            "[not-a-real-table]\n"
            'version = "=9.9.9"\n'
            '"""\n'
            'version = "=0.6.4"\n'
            'registry = "kin"\n'
        )
        path = self.write(
            "Cargo.toml",
            manifest,
        )
        with self.assertRaisesRegex(
            ucd.UpdateError, "physical multiline TOML strings"
        ):
            ucd.update_manifest(str(path), "kin-model", "0.7.0")
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)


# Verbatim cargo output from kin-bench run 32198854347, where a git-pinned
# kin-context exact-requires the version the wave was moving off. Reproduced at
# kin-bench 2a14ae72. Keeping the literal text means the parser is tested
# against what cargo actually prints rather than a paraphrase of it.
BLOCKED_INLINE_REQUIREMENT = (
    'error: failed to select a version for the requirement `kin-model = "=0.7.8"`\n'
    "candidate versions found which didn't match: 0.7.9\n"
    "location searched: `kin` index\n"
    "required by package `kin-context v0.5.23 "
    "(https://github.com/kinlab-ai/kin.git"
    "?rev=9ccb6182ee53da00dbf1d76a330d682b7f9be5d2#9ccb6182)`\n"
    "    ... which satisfies git dependency `kin-context` (locked to 0.5.23) "
    "of package `kin-bench-engine v0.1.0 "
    "(/home/runner/work/kin-bench/kin-bench/crates/kin-bench-engine)`\n"
)

# The second shape cargo uses, captured while re-running the same roll with
# both coupled pins rewritten. Here the crate is named first and its
# requirement arrives several lines later.
BLOCKED_DEFERRED_REQUIREMENT = (
    "error: failed to select a version for `kin-db`.\n"
    "    ... required by package `kin-context v0.5.23 "
    "(https://github.com/kinlab-ai/kin.git"
    "?rev=9ccb6182ee53da00dbf1d76a330d682b7f9be5d2#9ccb6182)`\n"
    "    ... which satisfies git dependency `kin-context` (locked to 0.5.23) "
    "of package `kin-bench-cli v0.1.0 (/tmp/kin-bench/crates/kin-bench-cli)`\n"
    "versions that meet the requirements `=0.7.21` are: 0.7.21\n"
    "\n"
    "all possible versions conflict with previously selected packages\n"
    "\n"
    "  previously selected package `kin-db v0.7.36 (registry `kin`)`\n"
)


class BlockedRollDetection(unittest.TestCase):
    def test_inline_requirement_shape_names_crate_requirement_and_package(self):
        blocked = ucd.parse_blocked_roll(BLOCKED_INLINE_REQUIREMENT)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.crate, "kin-model")
        self.assertEqual(blocked.requirement, "=0.7.8")
        self.assertTrue(blocked.package.startswith("kin-context v0.5.23 "))

    def test_deferred_requirement_shape_names_crate_requirement_and_package(self):
        blocked = ucd.parse_blocked_roll(BLOCKED_DEFERRED_REQUIREMENT)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.crate, "kin-db")
        self.assertEqual(blocked.requirement, "=0.7.21")
        self.assertTrue(blocked.package.startswith("kin-context v0.5.23 "))

    def test_unrelated_cargo_failures_are_never_read_as_blocked(self):
        # Each of these is a real way cargo fails that is NOT a blocked roll.
        # Classifying any of them as blocked would hide broken automation
        # behind a status that says the automation is fine.
        for output in (
            "",
            "error: no matching package named `kin-model` found\n",
            "error: failed to get `kin-model` as a dependency of package `x`\n"
            "Caused by:\n  failed to query replaced source registry `kin`\n",
            "error: could not write to file `Cargo.lock`: Permission denied\n",
            # Names a package but never states a requirement.
            "error: failed to select a version for `kin-db`.\n"
            "    ... required by package `kin-context v0.5.23 (git)`\n",
            # States a requirement but never names who imposes it.
            'error: failed to select a version for the requirement '
            '`kin-model = "=0.7.8"`\n'
            "candidate versions found which didn't match: 0.7.9\n",
        ):
            with self.subTest(output=output[:48]):
                self.assertIsNone(ucd.parse_blocked_roll(output))


class TransactionalUpdates(TemporaryManifestTest):
    def test_all_manifests_are_planned_before_any_write(self):
        first_text = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n'
        )
        second_text = (
            "[dependencies]\n"
            'kin-model = { version = ">=0.6, <0.7", registry = "kin" }\n'
        )
        first = self.write("Cargo.toml", first_text)
        second = self.write("crates/member/Cargo.toml", second_text)

        with self.assertRaisesRegex(
            ucd.UpdateError, "unsupported Cargo requirement"
        ):
            ucd.plan_manifests([first, second], {"kin-model": "0.7.0"})

        self.assertEqual(first.read_text(encoding="utf-8"), first_text)
        self.assertEqual(second.read_text(encoding="utf-8"), second_text)

    def test_multi_manifest_multi_crate_update_is_one_plan(self):
        first = self.write(
            "Cargo.toml",
            "[workspace.dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n',
        )
        second = self.write(
            "crates/member/Cargo.toml",
            "[dependencies]\n"
            'kin-db = { version = "~0.6.6", registry = "kin" }\n',
        )
        lock = self.write("Cargo.lock", "lock-before\n")
        calls = []

        def cargo_run(command, check, **_kwargs):
            calls.append((command, check))
            return subprocess.CompletedProcess(command, 0)

        versions = {"kin-model": "0.7.0", "kin-db": "0.6.7"}
        plans = ucd.plan_manifests([first, second], versions)
        changes = ucd.apply_plans(
            plans, versions, lock_path=lock, cargo_run=cargo_run
        )

        self.assertEqual(len(changes), 2)
        self.assertIn('version = "=0.7.0"', first.read_text(encoding="utf-8"))
        self.assertIn('version = "~0.6.7"', second.read_text(encoding="utf-8"))
        self.assertEqual(
            calls,
            [
                (
                    ["cargo", "update", "-p", "kin-model", "--precise", "0.7.0"],
                    True,
                ),
                (
                    ["cargo", "update", "-p", "kin-db", "--precise", "0.6.7"],
                    True,
                ),
            ],
        )

    def test_cargo_failure_restores_all_manifests_and_lockfile(self):
        first_text = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n'
        )
        second_text = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n'
        )
        first = self.write("Cargo.toml", first_text)
        second = self.write("crates/member/Cargo.toml", second_text)
        lock = self.write("Cargo.lock", "lock-before\n")

        def cargo_run(command, check, **_kwargs):
            lock.write_text("partially-updated-lock\n", encoding="utf-8")
            raise subprocess.CalledProcessError(101, command)

        versions = {"kin-model": "0.7.0"}
        plans = ucd.plan_manifests([first, second], versions)
        with self.assertRaisesRegex(
            ucd.UpdateError, "manifests and Cargo.lock restored"
        ):
            ucd.apply_plans(
                plans, versions, lock_path=lock, cargo_run=cargo_run
            )

        self.assertEqual(first.read_text(encoding="utf-8"), first_text)
        self.assertEqual(second.read_text(encoding="utf-8"), second_text)
        self.assertEqual(lock.read_text(encoding="utf-8"), "lock-before\n")

    def test_cargo_failure_recreates_deleted_lockfile(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        lock = self.write("Cargo.lock", "lock-before\n")
        lock.chmod(0o640)

        def cargo_run(command, check, **_kwargs):
            lock.unlink()
            raise subprocess.CalledProcessError(101, command)

        versions = {"kin-model": "0.7.0"}
        plans = ucd.plan_manifests([path], versions)
        with self.assertRaisesRegex(
            ucd.UpdateError, "manifests and Cargo.lock restored"
        ):
            ucd.apply_plans(
                plans, versions, lock_path=lock, cargo_run=cargo_run
            )

        self.assertEqual(path.read_text(encoding="utf-8"), manifest)
        self.assertEqual(lock.read_text(encoding="utf-8"), "lock-before\n")
        self.assertEqual(lock.stat().st_mode & 0o777, 0o640)

    def test_blocked_roll_raises_a_blocked_error_and_still_rolls_back(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "0.7.8", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        lock = self.write("Cargo.lock", "lock-before\n")

        def cargo_run(command, check, **_kwargs):
            lock.write_text("partially-updated-lock\n", encoding="utf-8")
            raise subprocess.CalledProcessError(
                101, command, output="", stderr=BLOCKED_INLINE_REQUIREMENT
            )

        versions = {"kin-model": "0.7.9"}
        plans = ucd.plan_manifests([path], versions)
        with self.assertRaises(ucd.BlockedRollError) as caught:
            ucd.apply_plans(
                plans, versions, lock_path=lock, cargo_run=cargo_run
            )

        message = str(caught.exception)
        self.assertIn("cannot roll kin-model to 0.7.9", message)
        self.assertIn("kin-context v0.5.23", message)
        self.assertIn("requires kin-model =0.7.8", message)
        self.assertIn("manifests and Cargo.lock restored", message)
        self.assertEqual(caught.exception.blocked.crate, "kin-model")
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)
        self.assertEqual(lock.read_text(encoding="utf-8"), "lock-before\n")

    def test_unrecognised_cargo_failure_stays_a_plain_update_error(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "0.7.8", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        lock = self.write("Cargo.lock", "lock-before\n")

        def cargo_run(command, check, **_kwargs):
            raise subprocess.CalledProcessError(
                101,
                command,
                output="",
                stderr="error: could not write to file `Cargo.lock`\n",
            )

        versions = {"kin-model": "0.7.9"}
        plans = ucd.plan_manifests([path], versions)
        with self.assertRaises(ucd.UpdateError) as caught:
            ucd.apply_plans(
                plans, versions, lock_path=lock, cargo_run=cargo_run
            )

        self.assertNotIsInstance(caught.exception, ucd.BlockedRollError)
        self.assertIn("dependency update failed", str(caught.exception))

    def test_cargo_output_reaches_the_log_on_success_and_on_failure(self):
        path = self.write(
            "Cargo.toml",
            "[dependencies]\n"
            'kin-model = { version = "0.7.8", registry = "kin" }\n',
        )
        lock = self.write("Cargo.lock", "lock-before\n")
        versions = {"kin-model": "0.7.9"}

        def succeeding(command, check, **_kwargs):
            return subprocess.CompletedProcess(
                command, 0, stdout="", stderr="    Updating kin index\n"
            )

        plans = ucd.plan_manifests([path], versions)
        with mock.patch("sys.stderr", new=io.StringIO()) as captured:
            ucd.apply_plans(
                plans, versions, lock_path=lock, cargo_run=succeeding
            )
        self.assertIn("Updating kin index", captured.getvalue())

        path.write_text(
            "[dependencies]\n"
            'kin-model = { version = "0.7.8", registry = "kin" }\n',
            encoding="utf-8",
        )

        def failing(command, check, **_kwargs):
            raise subprocess.CalledProcessError(
                101, command, output="", stderr=BLOCKED_INLINE_REQUIREMENT
            )

        plans = ucd.plan_manifests([path], versions)
        with mock.patch("sys.stderr", new=io.StringIO()) as captured:
            with self.assertRaises(ucd.BlockedRollError):
                ucd.apply_plans(
                    plans, versions, lock_path=lock, cargo_run=failing
                )
        self.assertIn("failed to select a version", captured.getvalue())

    def test_dry_run_never_writes_or_invokes_cargo(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        lock = self.write("Cargo.lock", "lock-before\n")

        def cargo_run(*_args, **_kwargs):
            raise AssertionError("cargo must not run in dry-run mode")

        versions = {"kin-model": "0.7.0"}
        plans = ucd.plan_manifests([path], versions)
        changes = ucd.apply_plans(
            plans,
            versions,
            dry_run=True,
            lock_path=lock,
            cargo_run=cargo_run,
        )

        self.assertEqual(len(changes), 1)
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)
        self.assertEqual(lock.read_text(encoding="utf-8"), "lock-before\n")

    def test_missing_manifest_aborts_planning(self):
        with self.assertRaisesRegex(ucd.UpdateError, "manifest does not exist"):
            ucd.plan_manifests(
                [self.root / "missing.toml"], {"kin-model": "0.7.0"}
            )

    def test_duplicate_manifest_arguments_are_deduplicated(self):
        path = self.write(
            "Cargo.toml",
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n',
        )
        plans = ucd.plan_manifests(
            [path, path, path.resolve()], {"kin-model": "0.7.0"}
        )
        self.assertEqual(len(plans), 1)


class CliIntegration(TemporaryManifestTest):
    script = Path(__file__).resolve().parent / "update-cargo-registry-deps.py"

    def setUp(self):
        super().setUp()
        self.registry = _RegistryServer(
            {"kin-model": registry_row(version="0.7.0")}
        )
        self.registry.__enter__()

    def tearDown(self):
        self.registry.__exit__(None, None, None)
        super().tearDown()

    def run_cli(self, *arguments, env=None):
        arguments = (*arguments, "--registry-url", self.registry.url)
        return subprocess.run(
            [sys.executable, str(self.script), *arguments],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_dry_run_cli_reports_without_mutating_manifest_or_lock(self):
        manifest = (
            "[dependencies]\n"
            "model={registry='kin',package='kin-model',version='=0.6.4'}\n"
        )
        path = self.write("Cargo.toml", manifest)
        lock = self.write("Cargo.lock", "lock-before\n")

        result = self.run_cli(
            "--crate",
            "kin-model",
            "--version",
            "0.7.0",
            "--dry-run",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry-run] would update", result.stdout)
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)
        self.assertEqual(lock.read_text(encoding="utf-8"), "lock-before\n")

    def test_surviving_event_refreshes_distinct_dropped_crate_event(self):
        self.registry.bodies["kin-model"] = registry_row(version="0.8.0")
        self.registry.bodies["kin-db"] = registry_row(
            crate="kin-db", version="0.6.7"
        )
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "=0.7.0", registry = "kin" }\n'
            'kin-db = { version = "=0.6.6", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)

        result = self.run_cli(
            "--crate",
            "kin-model",
            "--crate",
            "kin-db",
            "--event-crate",
            "kin-model",
            "--version",
            "0.8.0",
            "--dry-run",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kin-model -> 0.8.0", result.stdout)
        self.assertIn("kin-db -> 0.6.7", result.stdout)
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)

    def test_replayed_yanked_event_cannot_mutate_manifest_or_lock(self):
        self.registry.bodies["kin-model"] = b"".join(
            (
                registry_row(version="1.2.2"),
                registry_row(version="1.2.3", yanked=True),
            )
        )
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "=1.2.2", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        lock = self.write("Cargo.lock", "lock-before\n")

        result = self.run_cli(
            "--crate",
            "kin-model",
            "--event-crate",
            "kin-model",
            "--version",
            "1.2.3",
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("marks that exact version yanked", result.stdout)
        self.assertNotIn("updated ", result.stdout)
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)
        self.assertEqual(lock.read_text(encoding="utf-8"), "lock-before\n")

    def test_cli_preflight_failure_leaves_all_manifests_unchanged(self):
        first_text = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n'
        )
        second_text = (
            "[dependencies]\n"
            'kin-model = { version = ">=0.6, <0.7", registry = "kin" }\n'
        )
        first = self.write("Cargo.toml", first_text)
        second = self.write("crates/member/Cargo.toml", second_text)

        result = self.run_cli(
            "--crate",
            "kin-model",
            "--version",
            "0.7.0",
            "--manifest",
            "Cargo.toml",
            "--manifest",
            "crates/member/Cargo.toml",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("dependency wave aborted", result.stderr)
        self.assertEqual(first.read_text(encoding="utf-8"), first_text)
        self.assertEqual(second.read_text(encoding="utf-8"), second_text)

    def test_cli_cargo_failure_rolls_back_and_exits_nonzero(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        lock = self.write("Cargo.lock", "lock-before\n")
        fake_cargo = self.write(
            "bin/cargo",
            "#!/bin/sh\n"
            "python3 -c 'from pathlib import Path; Path(\"Cargo.lock\").unlink()'\n"
            "exit 101\n",
        )
        fake_cargo.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_cargo.parent}{os.pathsep}{environment['PATH']}"

        result = self.run_cli(
            "--crate",
            "kin-model",
            "--version",
            "0.7.0",
            env=environment,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("manifests and Cargo.lock restored", result.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)
        self.assertEqual(lock.read_text(encoding="utf-8"), "lock-before\n")

    def test_cli_blocked_roll_uses_exit_three_and_names_the_blocker(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "0.6.4", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        lock = self.write("Cargo.lock", "lock-before\n")
        conflict = self.write("conflict.txt", BLOCKED_INLINE_REQUIREMENT)
        fake_cargo = self.write(
            "bin/cargo",
            "#!/bin/sh\n" f"cat {conflict} >&2\n" "exit 101\n",
        )
        fake_cargo.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_cargo.parent}{os.pathsep}{environment['PATH']}"

        result = self.run_cli(
            "--crate",
            "kin-model",
            "--version",
            "0.7.0",
            env=environment,
        )

        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("dependency wave blocked:", result.stderr)
        self.assertIn("cannot roll kin-model to 0.7.0", result.stderr)
        self.assertIn("kin-context v0.5.23", result.stderr)
        self.assertIn("requires kin-model =0.7.8", result.stderr)
        # Cargo's own report still reaches the log, so a reader can check the
        # classification against the evidence it was drawn from.
        self.assertIn("failed to select a version", result.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)
        self.assertEqual(lock.read_text(encoding="utf-8"), "lock-before\n")

    def test_cli_no_change_uses_exit_two(self):
        self.write(
            "Cargo.toml",
            "[dependencies]\n"
            'kin-model = { version = "=0.7.0", registry = "kin" }\n',
        )
        result = self.run_cli(
            "--crate", "kin-model", "--version", "0.7.0"
        )
        self.assertEqual(result.returncode, 2, result.stderr)


class WorkflowContract(unittest.TestCase):
    def test_no_crates_is_a_no_change_exit(self):
        self.assertEqual(ucd.main([]), 2)

    def test_updater_failure_aborts_and_refresh_is_single_transaction(self):
        workflow_path = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/cargo-dependency-wave.yml"
        )
        workflow = workflow_path.read_text(encoding="utf-8")

        self.assertIn('case "$result" in', workflow)
        self.assertIn('0|2) return "$result"', workflow)
        self.assertIn('exit "$result"', workflow)
        self.assertIn('crate_args+=(--crate "$crate")', workflow)
        self.assertEqual(
            workflow.count(
                "python3 .kin-actions/scripts/update-cargo-registry-deps.py"
            ),
            1,
        )

    def run_update_function(self):
        """Return the workflow's own run_update body, so the tests run the shipped bytes."""

        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/cargo-dependency-wave.yml"
        ).read_text(encoding="utf-8")
        lines = workflow.splitlines()
        start = next(
            index for index, line in enumerate(lines) if line.strip() == "run_update() {"
        )
        end = next(
            index
            for index in range(start + 1, len(lines))
            if lines[index].strip() == "}"
        )
        return textwrap.dedent("\n".join(lines[start : end + 1]))

    def test_exact_workflow_function_propagates_exit_status(self):
        function = self.run_update_function()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / ".kin-actions/scripts/update-cargo-registry-deps.py"
            helper.parent.mkdir(parents=True)
            helper.write_text(
                "import os\nraise SystemExit(int(os.environ['FAKE_UPDATE_RC']))\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()

            for status in (0, 2):
                with self.subTest(status=status):
                    environment["FAKE_UPDATE_RC"] = str(status)
                    result = subprocess.run(
                        [
                            "bash",
                            "-c",
                            function
                            + "\nset +e\nrun_update\n"
                            + 'code=$?\nset -e\nprintf "code=%s\\n" "$code"\n',
                        ],
                        cwd=root,
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"code={status}", result.stdout)

            environment["FAKE_UPDATE_RC"] = "1"
            failed = subprocess.run(
                ["bash", "-c", function + "\nset +e\nrun_update\n"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(failed.returncode, 1)
            self.assertIn("aborting without a PR", failed.stdout)

    def test_exact_workflow_function_reports_a_blocked_roll_by_name(self):
        function = self.run_update_function()
        reason = (
            "cannot roll kin-model to 0.7.9: package kin-context v0.5.23 "
            "(https://github.com/kinlab-ai/kin.git?rev=9ccb6182) requires "
            "kin-model =0.7.8; manifests and Cargo.lock restored"
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / ".kin-actions/scripts/update-cargo-registry-deps.py"
            helper.parent.mkdir(parents=True)
            helper.write_text(
                "import sys\n"
                f"print({'dependency wave blocked: ' + reason!r}, file=sys.stderr)\n"
                "raise SystemExit(3)\n",
                encoding="utf-8",
            )
            summary = root / "summary.md"
            summary.touch()
            environment = os.environ.copy()
            environment["GITHUB_STEP_SUMMARY"] = str(summary)

            blocked = subprocess.run(
                ["bash", "-c", function + "\nset +e\nrun_update\n"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(blocked.returncode, 3, blocked.stdout)
            self.assertNotIn("aborting without a PR", blocked.stdout)
            self.assertIn(
                "::error title=Kin dependency wave blocked::", blocked.stdout
            )
            self.assertIn(reason, blocked.stdout)
            written = summary.read_text(encoding="utf-8")
            self.assertIn("Kin dependency wave blocked", written)
            self.assertIn(reason, written)

    def test_a_blocked_status_without_a_reason_stays_a_hard_failure(self):
        # The whole point of status 3 is that it names the blocker. A run that
        # claims to be blocked and cannot say why must not be reported as a
        # healthy workflow waiting on someone else.
        function = self.run_update_function()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / ".kin-actions/scripts/update-cargo-registry-deps.py"
            helper.parent.mkdir(parents=True)
            helper.write_text("raise SystemExit(3)\n", encoding="utf-8")
            summary = root / "summary.md"
            summary.touch()
            environment = os.environ.copy()
            environment["GITHUB_STEP_SUMMARY"] = str(summary)

            result = subprocess.run(
                ["bash", "-c", function + "\nset +e\nrun_update\n"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("without a reason line", result.stdout)
            self.assertEqual(summary.read_text(encoding="utf-8"), "")

    def test_lock_is_resynchronized_before_the_generated_delta_is_snapshotted(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/cargo-dependency-wave.yml"
        ).read_text(encoding="utf-8")

        resync = workflow.index("Resynchronize Cargo.lock with the bumped own version")
        bump = workflow.index("Bump own version so the PR can pass the version gate")
        snapshot = workflow.index("Snapshot the exact generated dependency delta")
        verification = workflow.index("- name: Run verification")

        # The bump desynchronizes the lock and the caller's test command
        # resolves it back, so the sync only works between those two points.
        self.assertLess(bump, resync)
        self.assertLess(resync, snapshot)
        self.assertLess(snapshot, verification)
        self.assertIn("cargo update --workspace", workflow)
        # Creating a lock a repo does not track would leave an untracked file
        # the admission gate is right to reject.
        self.assertIn("git ls-files --error-unmatch Cargo.lock", workflow)


if __name__ == "__main__":
    unittest.main()
