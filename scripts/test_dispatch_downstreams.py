"""Regression tests for bounded downstream release dispatch."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("dispatch-downstreams.sh")
PYTHON_SCRIPT = Path(__file__).with_name("dispatch-downstreams.py")
SPEC = importlib.util.spec_from_file_location("dispatch_downstreams", PYTHON_SCRIPT)
assert SPEC is not None and SPEC.loader is not None
dispatch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dispatch
SPEC.loader.exec_module(dispatch)


class _DispatchServer:
    def __init__(self, statuses: list[int]):
        self.statuses = statuses
        self.requests: list[dict[str, object]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                outer.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": json.loads(body),
                    }
                )
                index = min(len(outer.requests) - 1, len(outer.statuses) - 1)
                status = outer.statuses[index]
                self.send_response(status)
                if status in (403, 429):
                    self.send_header("Retry-After", "0.001")
                self.end_headers()
                if status >= 400:
                    body = (
                        b"secondary rate limit test response"
                        if status == 403
                        else b"temporary test response"
                    )
                    self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except PermissionError as exc:
            # Binding loopback is refused only inside a sandbox that denies all
            # networking, such as the export validation stage. Ordinary runs,
            # including the Script unit tests check, still exercise this test.
            raise unittest.SkipTest(f"loopback sockets are denied here: {exc}") from exc
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_DispatchServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"


class DispatchIntegration(unittest.TestCase):
    def run_dispatch(
        self,
        statuses: list[int],
        *,
        repo: str = "kinlab-ai/kin-db",
        downstreams: list[object] | None = None,
        include_token: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "downstreams.json"
            manifest.write_text(
                json.dumps(
                    {
                        "downstreams": (
                            [{"repo": repo}] if downstreams is None else downstreams
                        )
                    }
                )
            )
            with _DispatchServer(statuses) as server:
                env = {
                    **os.environ,
                    "PACKAGE": "kin-model",
                    "VERSION": "0.7.0",
                    "GITHUB_REPOSITORY": "kinlab-ai/kin-model",
                    "GITHUB_SHA": "a" * 40,
                    "DOWNSTREAM_MANIFEST": str(manifest),
                    "KIN_GITHUB_API_URL": server.url,
                    "KIN_DISPATCH_MAX_ATTEMPTS": "4",
                    "KIN_DISPATCH_BASE_DELAY_SECONDS": "0.001",
                    "KIN_DISPATCH_MAX_DELAY_SECONDS": "0.001",
                    "KIN_DISPATCH_MAX_RETRY_WAIT_SECONDS": "0.1",
                    "KIN_DISPATCH_TIMEOUT_SECONDS": "2",
                    "RELEASE_TAG_STATUS": "already-present",
                }
                if include_token:
                    env["KIN_DOWNSTREAM_DISPATCH_TOKEN"] = "test-token"
                else:
                    env.pop("KIN_DOWNSTREAM_DISPATCH_TOKEN", None)
                    env.pop("KIN_CI_BOT_TOKEN", None)
                    env.pop("GH_TOKEN", None)
                result = subprocess.run(
                    ["bash", str(SCRIPT)],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                return result, list(server.requests)

    def test_retries_429_and_5xx_then_succeeds(self) -> None:
        result, requests = self.run_dispatch([429, 503, 204])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(requests), 3)
        self.assertEqual({json.dumps(row["body"], sort_keys=True) for row in requests}, {
            json.dumps(
                {
                    "event_type": "kin-registry-release",
                    "client_payload": {
                        "crate_name": "kin-model",
                        "crate_version": "0.7.0",
                        "delivery_id": (
                            f"kinlab-ai/kin-model@{'a' * 40}:kin-model@0.7.0"
                        ),
                        "source_repo": "kinlab-ai/kin-model",
                        "source_sha": "a" * 40,
                    },
                },
                sort_keys=True,
            )
        })
        self.assertTrue(
            all(row["authorization"] == "Bearer test-token" for row in requests)
        )
        self.assertIn("registry publication and fresh-consumer proof are complete", result.stdout)
        self.assertIn("release-tag status=already-present", result.stdout)
        self.assertIn("attempts=3", result.stdout)

    def test_retries_rate_limited_403_then_succeeds(self) -> None:
        result, requests = self.run_dispatch([403, 204])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(requests), 2)
        self.assertIn("HTTP 403", result.stdout)

    def test_retry_after_is_not_shortened_to_local_backoff_cap(self) -> None:
        self.assertEqual(
            dispatch._retry_after_seconds(
                {"Retry-After": "120"},
                attempt=1,
                base_delay=1,
                max_delay=15,
                jitter=lambda: 0,
            ),
            120,
        )

    def test_long_server_delay_fails_without_early_retry(self) -> None:
        with self.assertRaisesRegex(dispatch.DispatchError, "refusing to retry early"):
            dispatch._consume_retry_budget(
                repo="kinlab-ai/kin-db",
                delay=120,
                retry_deadline=30,
                clock=lambda: 0,
            )

    def test_all_manifest_targets_share_one_retry_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "downstreams.json"
            manifest.write_text(
                json.dumps(
                    {
                        "downstreams": [
                            {"repo": "kinlab-ai/kin-db"},
                            {"repo": "kinlab-ai/kin"},
                        ]
                    }
                )
            )
            with mock.patch.dict(
                os.environ,
                {
                    "KIN_DOWNSTREAM_DISPATCH_TOKEN": "test-token",
                    "KIN_DISPATCH_MAX_RETRY_WAIT_SECONDS": "30",
                },
                clear=True,
            ), mock.patch.object(
                dispatch.time, "monotonic", return_value=100
            ), mock.patch.object(
                dispatch, "_dispatch_one", return_value=1
            ) as dispatch_one:
                dispatch.dispatch_manifest(
                    manifest,
                    "kin-model",
                    "0.7.0",
                    "kinlab-ai/kin-model",
                    "a" * 40,
                )
        self.assertEqual(dispatch_one.call_count, 2)
        self.assertEqual(
            {call.kwargs["retry_deadline"] for call in dispatch_one.call_args_list},
            {130},
        )

    def test_earlier_target_wait_reduces_later_target_budget(self) -> None:
        now = [0.0]
        deadline = 30.0
        dispatch._consume_retry_budget(
            repo="kinlab-ai/kin-db",
            delay=20,
            retry_deadline=deadline,
            clock=lambda: now[0],
        )
        now[0] = 20.0
        with self.assertRaisesRegex(
            dispatch.DispatchError, "remaining 10.0s retry wait budget"
        ):
            dispatch._consume_retry_budget(
                repo="kinlab-ai/kin",
                delay=11,
                retry_deadline=deadline,
                clock=lambda: now[0],
            )

    def test_non_transient_failure_is_not_retried(self) -> None:
        result, requests = self.run_dispatch([422, 204])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(requests), 1)
        self.assertIn("HTTP 422", result.stderr)
        self.assertIn("Registry publication is already durable", result.stderr)

    def test_transient_failures_stop_at_bound(self) -> None:
        result, requests = self.run_dispatch([500, 502, 503, 504, 204])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(requests), 4)
        self.assertIn("failed after 4 attempts", result.stderr)

    def test_invalid_repository_fails_before_network(self) -> None:
        result, requests = self.run_dispatch([204], repo="../../elsewhere")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(requests, [])
        self.assertIn("invalid downstream repository", result.stderr)

    def test_nonempty_manifest_without_token_fails_closed(self) -> None:
        result, requests = self.run_dispatch([204], include_token=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(requests, [])
        self.assertIn("no dispatch credential", result.stderr)

    def test_empty_manifest_without_token_is_a_noop(self) -> None:
        result, requests = self.run_dispatch(
            [204], downstreams=[], include_token=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(requests, [])
        self.assertIn("manifest is empty", result.stdout)


if __name__ == "__main__":
    unittest.main()
