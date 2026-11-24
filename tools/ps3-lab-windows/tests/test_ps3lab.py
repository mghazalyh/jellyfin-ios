"""PS3 parity checks run on GitHub-hosted Linux and Windows runners."""
import unittest
from urllib.parse import parse_qs, urlsplit
from ps3lab.jellyfin import JellyfinClient, PlaybackSession, build_stream_url, ps3_device_profile
from ps3lab.model import (
    Profile, NetworkShaper, Ps3Pipeline, TsInspector,
    frame_buffer_bytes, jitter_slots, live_buffer_target,
    ps3_memory_budget, simulate_scenario,
)
from ps3lab.sourcegate import SurfAudioGate, is_scene_cut, sequence_reached


def pkt(pid, cc, payload=b"", pusi=False):
    header = bytearray([0x47, ((pid >> 8) & 31) | (0x40 if pusi else 0),
                        pid & 255, 0x10 | (cc & 15)])
    header.extend(payload[:184])
    header.extend(b"\xff" * (188 - len(header)))
    return bytes(header)


class Ps3ForkParityTests(unittest.TestCase):
    def test_identity_matches_installed_console_without_stealing_its_id(self):
        client = JellyfinClient("http://127.0.0.1:8096")
        self.assertIn('Client="PS3"', client.authorization())
        self.assertIn('Device="PS3"', client.authorization())
        self.assertIn('Version="0.1"', client.authorization())
        self.assertEqual(client.headers()["User-Agent"], "JellyfinPS3/0.1")
        self.assertTrue(client.device_id.startswith("ps3-lab-"))
        self.assertNotEqual(client.device_id, "ps3")

    def test_actual_http_request_identifies_itself_as_ps3(self):
        """Verify wire-visible identity, not merely string-builder output."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        seen = {}

        class FakeJellyfin(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["method"] = self.command
                seen["path"] = self.path
                seen["auth"] = self.headers.get("Authorization", "")
                seen["user_agent"] = self.headers.get("User-Agent", "")
                response = b'{"Version":"10.11.10","ServerName":"Fixture"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *_args):
                pass

        fixture = ThreadingHTTPServer(("127.0.0.1", 0), FakeJellyfin)
        thread = Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        try:
            client = JellyfinClient(
                f"http://127.0.0.1:{fixture.server_port}",
                device_id="ps3-lab-fixture-device"
            )
            info = client.request("/System/Info/Public")
            self.assertEqual(info["ServerName"], "Fixture")
            self.assertEqual(seen["method"], "GET")
            self.assertEqual(seen["path"], "/System/Info/Public")
            self.assertIn('Client="PS3"', seen["auth"])
            self.assertIn('Device="PS3"', seen["auth"])
            self.assertIn('DeviceId="ps3-lab-fixture-device"', seen["auth"])
            self.assertIn('Version="0.1"', seen["auth"])
            self.assertEqual(seen["user_agent"], "JellyfinPS3/0.1")
        finally:
            fixture.shutdown()
            fixture.server_close()
            thread.join(timeout=2)

    def test_profile_exact_default_720p_stereo_behavior(self):
        profile = ps3_device_profile()
        self.assertEqual(profile["Name"], "PS3")
        self.assertEqual(profile["MaxStreamingBitrate"], 8000000)
        self.assertEqual(profile["DirectPlayProfiles"], [])
        transcode = profile["TranscodingProfiles"][0]
        self.assertEqual(transcode["Container"], "ts")
        self.assertEqual(transcode["VideoCodec"], "h264")
        self.assertEqual(transcode["AudioCodec"], "mp3")
        self.assertEqual(transcode["MaxAudioChannels"], "2")
        video = profile["CodecProfiles"][0]["Conditions"]
        self.assertTrue(any(x["Property"] == "Width" and x["Value"] == "1280" for x in video))
        self.assertTrue(any(x["Property"] == "Height" and x["Value"] == "720" for x in video))
        self.assertEqual(ps3_device_profile(True)["MaxStreamingBitrate"], 10000000)

    def test_stream_path_and_live_copy_safety(self):
        url = build_stream_url(
            "http://localhost:8096", "channel-id", "source-id", "ps3-lab-a",
            "playid", "livestream", live=True)
        self.assertEqual(urlsplit(url).path, "/Videos/channel-id/stream.ts")
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["VideoCodec"], ["h264"])
        self.assertEqual(query["AllowVideoStreamCopy"], ["false"])
        self.assertEqual(query["MediaSourceId"], ["source-id"])
        self.assertEqual(query["PlaySessionId"], ["playid"])
        self.assertEqual(query["LiveStreamId"], ["livestream"])
        self.assertEqual(query["Static"], ["false"])
        self.assertEqual(query["MaxFramerate"], ["30"])
        self.assertEqual(query["AudioCodec"], ["mp3"])
        self.assertEqual(query["Profile"], ["high"])
        self.assertEqual(query["Level"], ["41"])

    def test_session_playstate_does_not_send_live_id_on_start(self):
        class Recorder(JellyfinClient):
            def request(self, path, method="GET", payload=None, timeout=None):
                return (path, method, payload)
        client = Recorder("http://localhost:8096")
        session = PlaybackSession("item", "media", "playid", "live-id", True,
                                  "channel", "http://localhost/stream")
        start = client.playstate(session, "start")
        self.assertEqual(start[0], "/Sessions/Playing")
        self.assertNotIn("LiveStreamId", start[2])
        self.assertEqual(start[2]["PositionTicks"], 0)
        self.assertFalse(start[2]["CanSeek"])
        progress = client.playstate(session, "progress")
        self.assertNotIn("LiveStreamId", progress[2])
        stop = client.playstate(session, "stopped")
        self.assertEqual(stop[2]["LiveStreamId"], "live-id")

    def test_frame_slot_buffer_matches_fork(self):
        self.assertEqual(frame_buffer_bytes(1920, 1080), 1920 * 1088 * 3 // 2)
        self.assertEqual(jitter_slots(1920, 1080), 8)
        self.assertEqual(jitter_slots(1280, 720), 24)
        full = ps3_memory_budget(1920, 1080)
        self.assertEqual(full["prefill_frames"], 8)
        self.assertLessEqual(full["live_ring_bytes"], 10 * 1024 * 1024)
        self.assertEqual(live_buffer_target(4000000, 2000, 8), 1800)

    def test_wifi_simulates_retries_without_corrupting_ts(self):
        mode = Profile.from_switches(False, True)
        wifi = NetworkShaper(mode)
        one = wifi.delay_for(12032, now=10.0)
        two = wifi.delay_for(12032, now=10.0)
        self.assertGreater(one, 0)
        self.assertGreater(two, one)
        self.assertEqual(wifi.stats()["simulated_wifi_bytes"], 24064)
        self.assertEqual(NetworkShaper(Profile.from_switches(False, False)).delay_for(12032), 0)

    def test_declared_effective_wifi_goodput_survives_retry_pauses(self):
        # A retry must delay one chunk and then repay the pause as a catch-up
        # burst. Otherwise the shaper silently delivers far less than the
        # stated effective throughput (measured: 2047/702 kbps before fix).
        for severe, expected in ((False, 2500), (True, 1400)):
            shaper = NetworkShaper(Profile.from_switches(False, True, severe))
            now = 0.0
            sent = 0
            while now < 120.0:
                now += shaper.delay_for(12032, now=now)
                sent += 12032
            goodput = sent * 8.0 / now / 1000.0
            self.assertAlmostEqual(goodput, expected, delta=expected * 0.05)
            self.assertGreater(
                shaper.stats()["simulated_wifi_retransmission_pauses"], 0)

    def test_av_sync_keeps_small_negative_delta_negative(self):
        from ps3lab.model import av_sync_ms
        # Reproduced on real H.264/MP3 transport: video 1204322, audio
        # 1206000 ticks used to be reported as 47721840.2 ms.
        self.assertAlmostEqual(av_sync_ms(1204322, 1206000), -18.64, places=2)
        self.assertAlmostEqual(av_sync_ms(1206000, 1204322), 18.64, places=2)
        # Genuine 33-bit wrap: video just after zero, audio just before wrap.
        self.assertAlmostEqual(av_sync_ms(100, (1 << 33) - 100), 2.22, places=2)
        self.assertIsNone(av_sync_ms(None, 1))

    def test_ts_inspector_cc_and_split_chunks(self):
        tracker = TsInspector()
        data = pkt(0x101, 0) + pkt(0x101, 1) + pkt(0x101, 5) + pkt(0x1fff, 0)
        tracker.feed(data[:107])
        tracker.feed(data[107:])
        snap = tracker.snapshot()
        self.assertEqual(snap["ts_packets"], 4)
        self.assertEqual(snap["continuity_errors"], 1)
        self.assertEqual(snap["null_packets"], 1)
        self.assertEqual(snap["unparsed_tail_bytes"], 0)

    def test_pipeline_independently_toggleable(self):
        cpu = Profile.from_switches(True, False)
        wifi = Profile.from_switches(False, True)
        both = Profile.from_switches(True, True, True)
        self.assertTrue(cpu.simulate_cpu_gpu)
        self.assertFalse(cpu.simulate_wifi_b)
        self.assertTrue(wifi.simulate_wifi_b)
        self.assertFalse(wifi.simulate_cpu_gpu)
        self.assertEqual(both.effective_wifi_kbps, 1400)
        pipe = Ps3Pipeline(cpu)
        out = pipe.step(500, 10 * 1024 * 1024)
        self.assertEqual(out["simulated_frame_slots"], 8)
        self.assertGreater(out["simulated_vblanks"], 20)
        self.assertTrue(out["simulated_cpu_gpu_enabled"])
        data = simulate_scenario(both, seconds=2)
        self.assertEqual(data["hardware_emulator"], False)
        self.assertIn("simulated_presented_frames", data["simulation"])

    def test_scene_gate_requires_real_presentation_ack(self):
        gate = SurfAudioGate()
        gate.session(4)
        gate.set_enabled(True)
        self.assertEqual(gate.observe(3, "a", "boot"), "ignored")
        self.assertEqual(gate.observe(4, "a", "boot"), "initial_source")
        self.assertEqual(gate.observe(4, "b", "boot"), "transition_observed")
        self.assertEqual(gate.tick(100), "audio_gate_started")
        self.assertTrue(gate.gating)
        self.assertEqual(gate.decoded_picture(1, bytes(64)), "unchanged")
        self.assertEqual(gate.decoded_picture(2, bytes([230] * 64)), "visual_cut_decoded")
        self.assertEqual(gate.flip_complete(False, 2), "unchanged")
        self.assertTrue(gate.gating)
        self.assertEqual(gate.flip_complete(True, 1), "unchanged")
        self.assertEqual(gate.flip_complete(True, 2), "video_flip_audio_release")
        self.assertFalse(gate.gating)

    def test_scene_gate_timeout_safety(self):
        gate = SurfAudioGate()
        gate.session(1)
        gate.set_enabled(True)
        gate.observe(1, "source-a", "boot-1")
        gate.observe(1, "source-b", "boot-1")
        gate.tick(2000)
        self.assertEqual(gate.tick(4999), "unchanged")
        self.assertEqual(gate.tick(5000), "gate_timeout_recovery")
        self.assertFalse(gate.gating)
        self.assertTrue(is_scene_cut(bytes(64), bytes([200] * 64))[0])
        self.assertFalse(is_scene_cut(bytes(64), bytes([20] * 64))[0])
        self.assertTrue(sequence_reached(0, 0xffffffff))


if __name__ == "__main__":
    unittest.main()
