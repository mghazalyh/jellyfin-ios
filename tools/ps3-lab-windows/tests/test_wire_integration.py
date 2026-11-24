"""End-to-end PS3 spoofing against a local HTTP Jellyfin protocol fixture.

No user machines or live servers are accessed. The fixture exercises login,
token authentication, library, PlaybackInfo, real loopback proxy transport,
long source IDs, live/VOD sessions, progress, safety and error sanitization.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

from ps3lab.jellyfin import JellyfinClient, JellyfinError
from ps3lab.model import Profile
from ps3lab.proxy import StreamController, LocalProxy

DEVICE = "ps3-lab-live-protocol-fixture"
TOKEN = "fixture-private-token-should-never-be-logged"
LIVE = "live-id-" + "x" * 95
SESSION = "play-session-preserve-across-calls"
VIDEO = "movie-source/version+1"
CHANNEL = "live-channel"


class ServerState:
    def __init__(self):
        self.lock = threading.Lock()
        self.records = []
        self.fail_request = ""
        self.token = TOKEN
        self.mode = "normal"

    def record(self, method, path, headers, payload):
        with self.lock:
            self.records.append({
                "method": method, "path": path,
                "auth": headers.get("Authorization", ""),
                "user_agent": headers.get("User-Agent", ""),
                "payload": payload,
            })

    def snapshot(self):
        with self.lock:
            return list(self.records)


def pkt(pid, cc):
    return bytes([0x47, (pid >> 8) & 0x1f, pid & 0xff,
                  0x10 | (cc & 15)]) + bytes([cc & 255] * 184)


class ProtocolServer(ThreadingHTTPServer):
    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.state = ServerState()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        pass

    def respond(self, payload, status=200):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def verify(self):
        header = self.headers.get("Authorization", "")
        valid = all(fragment in header for fragment in (
            'Client="PS3"', 'Device="PS3"',
            'DeviceId="' + DEVICE + '"', 'Version="0.1"',
        ))
        return valid and self.headers.get("User-Agent") == "JellyfinPS3/0.1"

    def receive(self):
        path = urlsplit(self.path).path
        length = min(256 * 1024, max(0, int(self.headers.get("Content-Length", "0"))))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeError):
            body = None
        self.server.state.record(self.command, self.path, self.headers, body)
        if not self.verify():
            self.respond({"Message": "Identity mismatch"}, 403)
            return None, None
        if (path != "/Users/AuthenticateByName" and
            'Token="' + TOKEN + '"' not in self.headers.get("Authorization", "")):
            self.respond({"Message": "Invalid token"}, 401)
            return None, None
        if path == self.server.state.fail_request:
            self.respond({"Message": "fixture: simulated failure"}, 503)
            return None, None
        return path, body

    def do_POST(self):
        path, payload = self.receive()
        if path is None:
            return
        if path == "/Users/AuthenticateByName":
            if payload != {"Username": "fixture", "Pw": "password"}:
                return self.respond({}, 401)
            return self.respond({
                "AccessToken": TOKEN, "User": {"Id": "user-1", "Name": "Fixture"}
            })
        if path.endswith("/PlaybackInfo"):
            data = {
                "PlaySessionId": SESSION,
                "MediaSources": [
                    {"Id": LIVE if path.endswith(CHANNEL + "/PlaybackInfo") else VIDEO,
                     "LiveStreamId": LIVE if path.endswith(CHANNEL + "/PlaybackInfo") else ""}
                ],
            }
            return self.respond(data)
        if path.startswith("/Sessions/Playing"):
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self.respond({"Message": "Missing fixture endpoint"}, 404)

    def do_GET(self):
        path, payload = self.receive()
        if path is None:
            return
        if path == "/Users/Me":
            return self.respond({"Id": "user-1", "Name": "Fixture"})
        if path == "/LiveTv/Channels":
            return self.respond({"Items": [{
                "Id": CHANNEL, "Name": "Fixture TV", "Type": "TvChannel"
            }], "TotalRecordCount": 1})
        if path.startswith("/Users/") and "/Items" in path:
            return self.respond({"Items": [{
                "Id": "movie-1", "Name": "Fixture Film", "Type": "Movie",
                "RunTimeTicks": 36000000000,
            }], "TotalRecordCount": 1})
        if path.endswith("/stream.ts"):
            query = parse_qs(urlsplit(self.path).query)
            if "VideoCodec" not in query:
                return self.respond({}, 400)
            # A complete byte-preserving fixture; decoding is tested separately
            # by the native Windows mpv video smoke with real H.264.
            data = b"".join(pkt(256, i % 16) for i in range(512))
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (OSError, ConnectionError):
                pass
            return
        return self.respond({"Message": "Missing fixture endpoint"}, 404)


class PS3WireContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ProtocolServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        with self.server.state.lock:
            self.server.state.records.clear()
        self.server.state.fail_request = ""
        self.client = JellyfinClient(self.url, device_id=DEVICE)
        self.client.authenticate("fixture", "password")
        self.assertEqual(self.client.token, TOKEN)
        self.assertEqual(self.client.user_id, "user-1")

    def records(self, prefix=""):
        entries = self.server.state.snapshot()
        return [entry for entry in entries if urlsplit(entry["path"]).path.startswith(prefix)]

    def assert_identity(self, rows):
        self.assertTrue(rows)
        for r in rows:
            self.assertIn('Client="PS3"', r["auth"])
            self.assertIn('Device="PS3"', r["auth"])
            self.assertIn('DeviceId="' + DEVICE + '"', r["auth"])
            self.assertIn('Version="0.1"', r["auth"])
            self.assertEqual(r["user_agent"], "JellyfinPS3/0.1")

    def test_login_and_wire_auth(self):
        self.assert_identity(self.records())
        self.assertNotIn(TOKEN, self.records()[0]["auth"])
        self.client.request("Users/Me")
        self.assertIn('Token="' + TOKEN + '"', self.records()[-1]["auth"])
        new = JellyfinClient(self.url, device_id=DEVICE)
        new.authenticate_token(TOKEN)
        self.assertEqual(new.user_id, "user-1")
        self.assert_identity(self.records())

    def test_browse_and_playbackinfo_spoof(self):
        self.assertEqual(self.client.browse("movies")["TotalRecordCount"], 1)
        self.assertEqual(len(self.client.browse("live")["Items"]), 1)
        movie = self.client.make_session({"Id": "movie-1", "Type": "Movie"})
        self.assertEqual(movie.play_session_id, SESSION)
        self.assertEqual(movie.source_id, VIDEO)
        data = self.records("/Items/")[-1]
        self.assertEqual(data["method"], "POST")
        profile = data["payload"]["DeviceProfile"]
        self.assertEqual(profile["Name"], "PS3")
        self.assertEqual(profile["TranscodingProfiles"][0]["Container"], "ts")
        self.assertEqual(profile["TranscodingProfiles"][0]["VideoCodec"], "h264")
        self.assert_identity(self.records())

    def test_vod_playback_session_lifecycle_and_resume(self):
        item = {"Id": "movie-1", "Type": "Movie"}
        session = self.client.make_session(item, start_ticks=5000000000)
        self.client.playstate(session, "start")
        self.client.playstate(session, "progress", pos_ticks=5200000000, paused=True)
        self.client.playstate(session, "stopped", pos_ticks=5400000000)
        records = self.records("/Sessions/Playing")
        self.assertEqual(len(records), 3)
        self.assertEqual([r["path"] for r in records], [
            "/Sessions/Playing",
            "/Sessions/Playing/Progress",
            "/Sessions/Playing/Stopped",
        ])
        for entry in records:
            self.assertEqual(entry["payload"]["PlaySessionId"], SESSION)
            self.assertEqual(entry["payload"]["ItemId"], "movie-1")
            self.assertNotIn("LiveStreamId", entry["payload"])
        self.assertTrue(records[1]["payload"]["IsPaused"])
        self.assertEqual(records[1]["payload"]["PositionTicks"], 5200000000)
        self.assertEqual(records[2]["payload"]["PositionTicks"], 5400000000)
        self.assertTrue(all(r["payload"]["CanSeek"] for r in records))

    def test_live_session_safety_and_long_livestream_id(self):
        item = {"Id": CHANNEL, "Type": "TvChannel"}
        session = self.client.make_session(item, surround=True)
        self.assertEqual(session.live_stream_id, LIVE)
        parsed = parse_qs(urlsplit(session.url).query)
        self.assertEqual(parsed["LiveStreamId"], [LIVE])
        self.assertEqual(parsed["AllowVideoStreamCopy"], ["false"])
        self.assertEqual(parsed["Profile"], ["high"])
        self.client.playstate(session, "start", pos_ticks=123)
        self.client.playstate(session, "progress", pos_ticks=400, paused=True)
        self.client.playstate(session, "stopped", pos_ticks=500)
        history = self.records("/Sessions/Playing")
        self.assertEqual(len(history), 3)
        self.assertNotIn("LiveStreamId", history[0]["payload"])
        self.assertNotIn("LiveStreamId", history[1]["payload"])
        self.assertEqual(history[2]["payload"]["LiveStreamId"], LIVE)
        self.assertEqual([r["payload"]["PositionTicks"] for r in history],
                         [0, 0, 0])
        self.assertTrue(all(not r["payload"]["CanSeek"] for r in history))

    def test_real_loopback_transport_forwards_exact_ps3_headers(self):
        session = self.client.make_session({"Id": "movie-1", "Type": "Movie"})
        events = []
        control = StreamController(lambda name, data: events.append((name, data)))
        control.configure(session.url, self.client.headers(),
                          Profile.from_switches(False, False), live=session.live)
        relay = LocalProxy(control)
        relay.start()
        try:
            with urllib.request.urlopen(relay.url, timeout=10) as reader:
                result = reader.read(188 * 512)
            self.assertEqual(len(result), 188 * 512)
            self.assertEqual(result[:4], pkt(256, 0)[:4])
            stats = control.inspector.snapshot()
            self.assertEqual(stats["ts_packets"], 512)
            self.assertEqual(stats["continuity_errors"], 0)
            self.assertTrue(any(name == "upstream_http" for name, _ in events))
            request = self.records("/Videos/")[-1]
            self.assert_identity([request])
            self.assertEqual(request["path"].split("?")[0],
                             "/Videos/movie-source%2Fversion%2B1/stream.ts")
            self.assertEqual(parse_qs(urlsplit(request["path"]).query)["DeviceId"],
                             [DEVICE])
            self.assertNotIn(TOKEN, relay.url)
            self.assertNotIn(DEVICE, relay.url)
        finally:
            relay.close()

    def test_invalid_token_401_returns_bounded_error(self):
        other = JellyfinClient(self.url, device_id=DEVICE)
        with self.assertRaises(JellyfinError) as context:
            other.authenticate_token("invalid-token")
        self.assertIn("HTTP 401", str(context.exception))
        self.assertNotIn("invalid-token", str(context.exception))
        self.assertEqual(other.token, "")

    def test_server_failure_is_detected_not_successful_empty_list(self):
        self.server.state.fail_request = "/LiveTv/Channels"
        with self.assertRaises(JellyfinError) as context:
            self.client.browse("live")
        self.assertIn("HTTP 503", str(context.exception))
        self.assertNotIn(TOKEN, str(context.exception))


if __name__ == "__main__":
    unittest.main()
