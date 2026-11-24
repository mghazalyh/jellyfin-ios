"""Verify the runtime resource model tracks PS3 requests, not host decoding."""
import unittest
from dataclasses import replace

from ps3lab.jellyfin import build_stream_url
from ps3lab.model import Profile, RING_MAX, LIVE_RING_MAX, frame_buffer_bytes
from ps3lab.proxy import StreamController, requested_stream_workload


class StreamWorkloadTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile.from_switches(False, False)

    def url(self, quality, **kwargs):
        return build_stream_url(
            "http://localhost:8096", "item", "source", "test-device",
            quality=quality, **kwargs)

    def test_all_seven_quality_presets(self):
        expected = (
            ("auto", 1280, 720, 4000000),
            ("360p", 640, 360, 700000),
            ("480p", 854, 480, 1500000),
            ("720p", 1280, 720, 4000000),
            ("1080p", 1920, 1080, 10000000),
            ("1080p20", 1920, 1080, 20000000),
            ("1080p25", 1920, 1080, 25000000),
        )
        for quality, width, height, rate in expected:
            with self.subTest(quality=quality):
                w, h, profile = requested_stream_workload(self.url(quality), self.profile)
                self.assertEqual((w, h), (width, height))
                self.assertEqual(profile.nominal_transport_bps, rate)
                self.assertEqual(profile.stream_video_bps, rate * 97 // 100 - 192000)

    def test_stress_switch_retains_requested_bitrate_and_frame_size(self):
        control = StreamController(lambda *_: None)
        control.configure(self.url("360p"), {}, self.profile, live=False)
        model = control.pipeline
        self.assertEqual(model.budget["video_frame_bytes"], frame_buffer_bytes(640, 360))
        self.assertEqual(model.step(0)["simulated_ring_capacity_bytes"], RING_MAX)
        control.set_profile(Profile.from_switches(True, True, severe=True))
        self.assertIs(control.pipeline, model)
        self.assertEqual(control.profile.nominal_transport_bps, 700000)
        self.assertEqual(model.profile.stream_video_bps, 487000)
        self.assertTrue(model.profile.simulate_wifi_b)
        self.assertTrue(model.profile.simulate_cpu_gpu)
        control.configure(self.url("1080p25", hd=True, live=True), {}, self.profile, live=True)
        self.assertEqual(control.pipeline.budget["decoded_slots"], 8)
        self.assertEqual(control.pipeline.step(0)["simulated_ring_capacity_bytes"], LIVE_RING_MAX)
        self.assertEqual(control.profile.nominal_transport_bps, 25000000)

    def test_explicit_low_quality_overrides_hd_toggle(self):
        w, h, profile = requested_stream_workload(
            self.url("480p", hd=True), self.profile, hd=True)
        self.assertEqual((w, h, profile.nominal_transport_bps), (854, 480, 1500000))

    def test_invalid_query_uses_bounded_defaults(self):
        url = "http://localhost/stream?MaxWidth=0&MaxHeight=999999&VideoBitrate=NaN&AudioBitrate=-1"
        w, h, profile = requested_stream_workload(url, self.profile)
        self.assertEqual((w, h), (1280, 720))
        self.assertEqual(profile.nominal_transport_bps, 4000000)
        self.assertEqual(profile.stream_transport_bps, 0)

    def test_passthrough_does_not_invent_audio_rate(self):
        _, _, profile = requested_stream_workload(
            self.url("720p", ac3_copy=True), self.profile)
        self.assertEqual(profile.stream_transport_bps, 0)
        self.assertEqual(profile.nominal_transport_bps, profile.stream_video_bps)

    def test_fixture_keeps_configured_profile(self):
        profile = replace(self.profile, stream_video_bps=1234567)
        w, h, actual = requested_stream_workload("http://localhost/fixture", profile, hd=True)
        self.assertEqual((w, h, actual.nominal_transport_bps), (1920, 1080, 1234567))

    def test_measured_sample_labels_model_ceiling(self):
        events = []
        control = StreamController(lambda name, values: events.append((name, values)))
        control.configure(self.url("360p"), {}, self.profile, live=False)
        control.ingest(bytes([0x47, 0x1f, 0xff, 0x10]) + bytes(184))
        sample = next(v for n, v in events if n == "sample")
        self.assertEqual(sample["simulated_requested_width"], 640)
        self.assertEqual(sample["simulated_requested_height"], 360)
        self.assertEqual(sample["simulated_nominal_transport_bps"], 700000)
        self.assertTrue(sample["simulated_transport_rate_includes_audio"])
        self.assertFalse(sample["simulated_live"])
        self.assertEqual(sample["ts_packets"], 1)


if __name__ == "__main__":
    unittest.main()
