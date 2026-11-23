"""Fixture regression for the portable real-Jellyfin readiness gate."""
from __future__ import annotations
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import threading
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import urlsplit

import live_server_gate as gate


class PublicHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path == "/System/Info/Public":
            data = json.dumps({"Version": "12.4.0", "ServerName": "fixture"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)
    def log_message(self, *_args):
        return


@contextmanager
def fixture():
    server = ThreadingHTTPServer(("127.0.0.1", 0), PublicHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)


class RealServerGateTests(unittest.TestCase):
    def test_public_health_has_no_auth_and_does_not_write(self):
        with fixture() as server:
            with patch.dict(os.environ, {
                "JELLYFIN_TEST_TOKEN": "",
                "JELLYFIN_TEST_USERNAME": "",
                "JELLYFIN_TEST_PASSWORD": "",
            }):
                result = gate.execute(server, False, False, "vod")
        self.assertTrue(result["passed"])
        self.assertEqual(result["checks"]["public"], True)
        self.assertEqual(result["scope"], "public-health")
        self.assertEqual(result["server_version"], "12.4.0")
        self.assertFalse(result["checks"]["authentication"])
        self.assertIn("no-JELLYFIN_TEST_TOKEN-or-test-user-credentials",
                      result["skipped"])

    def test_streaming_does_not_run_without_explicit_credentials(self):
        with fixture() as server, patch.dict(os.environ, {
            "JELLYFIN_TEST_TOKEN": "",
            "JELLYFIN_TEST_USERNAME": "",
            "JELLYFIN_TEST_PASSWORD": "",
        }):
            result = gate.execute(server, True, True, "live")
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["streaming"])
        self.assertEqual(result["failures"][0]["phase"], "authentication-required")
        self.assertEqual(result["failures"][0]["error_type"], "RuntimeError")

    def test_url_validation_blocks_auth_query_and_remote_paths(self):
        for url in ("http://user:password@127.0.0.1:8096",
                    "http://127.0.0.1:8096?token=secret",
                    "http://127.0.0.1:8096/library",
                    "ftp://127.0.0.1:8096",
                    "http://127.0.0.1:8096#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                gate.safe_public_info(url)

    def test_human_output_does_not_expose_secret_inputs(self):
        with fixture() as server:
            with patch.dict(os.environ, {
                "JELLYFIN_TEST_TOKEN": "",
                "JELLYFIN_TEST_USERNAME": "",
                "JELLYFIN_TEST_PASSWORD": "",
            }):
                captured = io.StringIO()
                with redirect_stdout(captured):
                    retcode = gate.main(["--server", server])
        self.assertEqual(retcode, 0)
        payload = json.loads(captured.getvalue())
        self.assertEqual(payload["schema"], "jellyfin-ps3-real-server-gate/v1")
        self.assertNotIn("token", captured.getvalue().lower().replace(
            "no-JELLYFIN_TEST_TOKEN-or-test-user-credentials".lower(), ""))

    def test_reports_use_only_safe_fixed_failure_fields(self):
        checks = gate.Checks()
        checks.fail("example", RuntimeError("secret:DO_NOT_LEAK /Users/realname"))
        payload = json.dumps(checks.as_dict(False, False))
        self.assertNotIn("DO_NOT_LEAK", payload)
        self.assertNotIn("/Users/", payload)
        self.assertIn("RuntimeError", payload)


if __name__ == "__main__":
    unittest.main()
