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
from types import SimpleNamespace
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

    def test_optional_seek_is_not_claimed_as_verified(self):
        class FakeClient:
            def __init__(self):
                self.events = []
            def make_session(self, *_args, **_kwargs):
                return SimpleNamespace(url="http://127.0.0.1/test", live=False,
                                       start_ticks=0, play_session_id="session",
                                       source_id="source")
            def playstate(self, _session, event, **_kwargs):
                self.events.append(event)
            def seek_session(self, *_args, **_kwargs):
                raise AssertionError("Unrequested seek must not run")

        client = FakeClient()
        checks = gate.Checks(public_ok=True, authentication_ok=True, library_ok=True, seek_requested=False)
        with patch.object(gate, "read_transport"):
            gate.run_stream_tests(client, {"Type": "Movie"}, checks, False)
        self.assertTrue(checks.success(True, True))
        self.assertTrue(checks.streaming_ok)
        self.assertFalse(checks.seek_ok)
        self.assertFalse(checks.as_dict(True, True)["checks"]["session_seek"])
        self.assertFalse(checks.as_dict(True, True)["checks"]["seek_requested"])
        self.assertIn("seek-not-requested", checks.skipped)
        self.assertEqual(client.events, ["start", "progress", "stopped"])

    def test_requested_seek_proves_session_continuity(self):
        class FakeClient:
            def __init__(self):
                self.events = []
                self.positions = []
                self.seeks = []
            def make_session(self, *_args, **_kwargs):
                return SimpleNamespace(url="http://127.0.0.1/test", live=False,
                                       start_ticks=123, play_session_id="session",
                                       source_id="source")
            def playstate(self, _session, event, **_kwargs):
                self.events.append(event)
                self.positions.append(_kwargs.get("pos_ticks"))
            def seek_session(self, session, ticks, **_kwargs):
                self.seeks.append(ticks)
                return SimpleNamespace(url=session.url, live=False,
                                       start_ticks=ticks,
                                       play_session_id=session.play_session_id,
                                       source_id=session.source_id)

        client = FakeClient()
        checks = gate.Checks(public_ok=True, authentication_ok=True, library_ok=True, seek_requested=True)
        with patch.object(gate, "read_transport") as read:
            gate.run_stream_tests(client, {"Type": "Movie", "RunTimeTicks": 500000000},
                                  checks, True)
        self.assertTrue(checks.success(True, True))
        self.assertTrue(checks.seek_ok)
        self.assertTrue(checks.as_dict(True, True)["checks"]["seek_requested"])
        self.assertEqual(client.seeks, [100000000])
        self.assertEqual(read.call_count, 2)
        self.assertEqual(client.events, ["start", "progress", "progress", "stopped"])
        self.assertEqual(client.positions, [0, 123, 100000000, 100000000])

    def test_stream_failure_still_reports_stopped_and_redacts_error(self):
        class FakeClient:
            def __init__(self):
                self.events = []
            def make_session(self, *_args, **_kwargs):
                return SimpleNamespace(url="http://127.0.0.1/test", live=False,
                                       start_ticks=0, play_session_id="session",
                                       source_id="source")
            def playstate(self, _session, event, **_kwargs):
                self.events.append(event)

        client = FakeClient()
        checks = gate.Checks(public_ok=True, authentication_ok=True, library_ok=True, seek_requested=True)
        with patch.object(gate, "read_transport",
                          side_effect=RuntimeError("private:UNSAFE_SECRET")):
            gate.run_stream_tests(client, {"Type": "Movie"}, checks, True)
        self.assertFalse(checks.success(True, True))
        self.assertEqual(client.events, ["start", "stopped"])
        self.assertEqual(checks.failures[0]["phase"], "authenticated-stream")
        self.assertNotIn("UNSAFE_SECRET", json.dumps(checks.as_dict(True, True)))

    def test_seek_continuity_mismatch_fails_and_stop_is_reported(self):
        for mismatch in ("play_session_id", "source_id"):
            with self.subTest(mismatch=mismatch):
                class FakeClient:
                    def __init__(self):
                        self.events = []
                    def make_session(self, *_args, **_kwargs):
                        return SimpleNamespace(url="http://127.0.0.1/test", live=False,
                                               start_ticks=0, play_session_id="session",
                                               source_id="source")
                    def playstate(self, _session, event, **_kwargs):
                        self.events.append(event)
                    def seek_session(self, session, ticks, **_kwargs):
                        return SimpleNamespace(
                            url=session.url, live=False, start_ticks=ticks,
                            play_session_id=("changed" if mismatch == "play_session_id"
                                             else session.play_session_id),
                            source_id=("changed" if mismatch == "source_id"
                                       else session.source_id))

                client = FakeClient()
                checks = gate.Checks(public_ok=True, authentication_ok=True,
                                     library_ok=True, seek_requested=True)
                with patch.object(gate, "read_transport"):
                    gate.run_stream_tests(client, {"Type": "Movie"}, checks, True)
                self.assertFalse(checks.success(True, True))
                self.assertFalse(checks.seek_ok)
                self.assertEqual(checks.failures[0]["phase"], "authenticated-stream")
                self.assertEqual(checks.failures[0]["error_type"], "RuntimeError")
                self.assertEqual(client.events, ["start", "progress", "stopped"])
                self.assertIn("Stopped session reported to Jellyfin",
                              checks.completed)

    def test_stop_report_failure_is_not_lifecycle_success(self):
        class FakeClient:
            def __init__(self):
                self.events = []
            def make_session(self, *_args, **_kwargs):
                return SimpleNamespace(url="http://127.0.0.1/test", live=False,
                                       start_ticks=0, play_session_id="session",
                                       source_id="source")
            def playstate(self, _session, event, **_kwargs):
                self.events.append(event)
                if event == "stopped":
                    raise RuntimeError("private:STOP_SECRET")

        client = FakeClient()
        checks = gate.Checks(public_ok=True, authentication_ok=True, library_ok=True)
        with patch.object(gate, "read_transport"):
            gate.run_stream_tests(client, {"Type": "Movie"}, checks, False)
        self.assertFalse(checks.success(True, True))
        self.assertTrue(checks.streaming_ok)
        self.assertFalse(checks.lifecycle_ok)
        self.assertEqual(client.events, ["start", "progress", "stopped"])
        self.assertEqual(checks.failures[-1]["phase"], "playstate-stop")
        self.assertEqual(checks.failures[-1]["error_type"], "RuntimeError")
        self.assertNotIn("STOP_SECRET", json.dumps(checks.as_dict(True, True)))

    def test_playbackinfo_failure_does_not_start_or_stop_a_session(self):
        class FakeClient:
            def __init__(self):
                self.events = []
            def make_session(self, *_args, **_kwargs):
                raise RuntimeError("private:PLAYBACKINFO_SECRET")
            def playstate(self, _session, event, **_kwargs):
                self.events.append(event)

        client = FakeClient()
        checks = gate.Checks(public_ok=True, authentication_ok=True, library_ok=True)
        gate.run_stream_tests(client, {"Type": "Movie"}, checks, False)
        self.assertFalse(checks.success(True, True))
        self.assertFalse(checks.streaming_ok)
        self.assertFalse(checks.lifecycle_ok)
        self.assertEqual(client.events, [])
        self.assertEqual(checks.failures[0]["phase"], "authenticated-stream")
        self.assertNotIn("PLAYBACKINFO_SECRET",
                         json.dumps(checks.as_dict(True, True)))

    def test_live_tv_never_seeks_and_reports_stop(self):
        class FakeClient:
            def __init__(self):
                self.events = []
            def make_session(self, *_args, **_kwargs):
                return SimpleNamespace(url="http://127.0.0.1/live", live=True,
                                       start_ticks=0, play_session_id="live-session",
                                       source_id="source")
            def playstate(self, _session, event, **_kwargs):
                self.events.append(event)
            def seek_session(self, *_args, **_kwargs):
                raise AssertionError("Live stream must not seek")

        client = FakeClient()
        checks = gate.Checks(public_ok=True, authentication_ok=True, library_ok=True, seek_requested=True)
        with patch.object(gate, "read_transport") as read:
            gate.run_stream_tests(client, {"Type": "TvChannel"}, checks, True)
        self.assertTrue(checks.success(True, True))
        self.assertTrue(checks.live_checked)
        self.assertFalse(checks.seek_ok)
        self.assertFalse(checks.as_dict(True, True)["checks"]["session_seek"])
        self.assertIn("live-tv-is-not-seekable", checks.skipped)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(client.events, ["start", "progress", "stopped"])


if __name__ == "__main__":
    unittest.main()
