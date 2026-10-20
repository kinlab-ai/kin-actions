"""End-to-end release collision tests for ``publish-crate.sh``."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT = Path(__file__).with_name("publish-crate.sh")
PAYLOAD = b"deterministic crate payload\n"
PAYLOAD_CHECKSUM = hashlib.sha256(PAYLOAD).hexdigest()


def index_row(*, checksum=PAYLOAD_CHECKSUM, yanked=False):
    return (
        json.dumps(
            {
                "name": "x",
                "vers": "1.2.3",
                "yanked": yanked,
                "cksum": checksum,
                "deps": [],
                "features": {},
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


class _RegistryServer:
    def __init__(self, *, body=None, post_status=409, after_post_body=None):
        self.body = body
        self.post_status = post_status
        self.after_post_body = after_post_body
        self.posts = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib callback
                body = (
                    outer.after_post_body
                    if outer.posts and outer.after_post_body is not None
                    else outer.body
                )
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802 - stdlib callback
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                outer.posts += 1
                self.send_response(outer.post_status)
                self.end_headers()

            def log_message(self, _format, *_args):
                return

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except PermissionError as exc:
            # Binding loopback is refused only inside a sandbox that denies all
            # networking, such as the export validation stage. Ordinary runs,
            # including the Script unit tests check, still exercise this test.
            raise unittest.SkipTest(f"loopback sockets are denied here: {exc}") from exc
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


class PublishCollision(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        cargo = fake_bin / "cargo"
        cargo.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "case \"$1\" in\n"
            "  metadata)\n"
            "    printf '%s\\n' '{\"packages\":[{\"name\":\"x\",\"version\":\"1.2.3\"}]}'\n"
            "    ;;\n"
            "  package)\n"
            "    mkdir -p target/package\n"
            "    printf 'deterministic crate payload\\n' > target/package/x-1.2.3.crate\n"
            "    ;;\n"
            "  *) exit 64 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        cargo.chmod(0o755)
        self.environment = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "PACKAGE": "x",
            "KINLAB_CARGO_TOKEN": "test-token",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def run_publish(self, server):
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=self.root,
            env={**self.environment, "KINLAB_CARGO_REGISTRY_URL": server.url},
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )

    def test_matching_immutable_artifact_is_idempotent(self):
        with _RegistryServer(body=index_row()) as server:
            result = self.run_publish(server)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(server.posts, 0)
        self.assertIn("matching checksum; no-op", result.stdout)

    def test_existing_different_artifact_fails_before_tag_authority(self):
        with _RegistryServer(body=index_row(checksum="1" * 64)) as server:
            result = self.run_publish(server)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(server.posts, 0)
        self.assertIn("different artifact", result.stderr)

    def test_yanked_immutable_version_cannot_be_reused(self):
        with _RegistryServer(body=index_row(yanked=True)) as server:
            result = self.run_publish(server)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(server.posts, 0)
        self.assertIn("published and yanked", result.stderr)

    def test_empty_successful_index_fails_closed(self):
        with _RegistryServer(body=b"\n") as server:
            result = self.run_publish(server)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(server.posts, 0)
        self.assertIn("empty successful response", result.stderr)

    def test_concurrent_conflict_with_different_artifact_fails_verification(self):
        with _RegistryServer(
            body=None,
            post_status=409,
            after_post_body=index_row(checksum="2" * 64),
        ) as server:
            result = self.run_publish(server)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(server.posts, 1)
        self.assertIn("published checksum mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
