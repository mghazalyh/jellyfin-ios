"""Verify the runtime resource model tracks PS3 requests, not host decoding."""
import unittest
from dataclasses import replace

from ps3lab.jellyfin import build_stream_url
from ps3lab.model import (
    Profile, RING_MAX, RING_MIN, LIVE_RING_MAX, TS_PACKET_SIZE,
    assumed_ring_capacity_bytes, frame_buffer_bytes,
)
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
        self.assertEqual(model.step(0)["simulated_ring_capacity_bytes"],
                         assumed_ring_capacity_bytes(self.profile.free_xdr_mib, False))
        control.set_profile(Profile.from_switches(True, True, severe=True))
        self.assertIs(control.pipeline, model)
        self.assertEqual(control.profile.nominal_transport_bps, 700000)
        self.assertEqual(model.profile.stream_video_bps, 487000)
        self.assertTrue(model.profile.simulate_wifi_b)
        self.assertTrue(model.profile.simulate_cpu_gpu)
        control.configure(self.url("1080p25", hd=True, live=True), {}, self.profile, live=True)
        self.assertEqual(control.pipeline.budget["decoded_slots"], 8)
        self.assertEqual(control.pipeline.step(0)["simulated_ring_capacity_bytes"],
                         assumed_ring_capacity_bytes(self.profile.free_xdr_mib, True))
        self.assertEqual(control.profile.nominal_transport_bps, 25000000)

    def test_low_resolution_live_uses_1080p_reserved_slots(self):
        control = StreamController(lambda *_: None)
        control.configure(self.url("360p"), {}, self.profile, live=True)
        pipe = control.pipeline
        self.assertEqual((pipe.requested_width, pipe.requested_height), (640, 360))
        self.assertEqual(pipe.budget["reserved_dimensions"], (1920, 1080))
        self.assertEqual(pipe.budget["video_frame_bytes"], frame_buffer_bytes(1920, 1080))
        self.assertEqual(pipe.budget["decoded_slots"], 8)
        stats = pipe.step(0)
        self.assertEqual((stats["simulated_requested_width"],
                          stats["simulated_requested_height"]), (640, 360))
        self.assertEqual((stats["simulated_reserved_width"],
                          stats["simulated_reserved_height"]), (1920, 1080))
        self.assertEqual(stats["simulated_ring_capacity_bytes"],
                         assumed_ring_capacity_bytes(self.profile.free_xdr_mib, True))

    def test_low_resolution_vod_keeps_requested_slots(self):
        control = StreamController(lambda *_: None)
        control.configure(self.url("360p"), {}, self.profile, live=False)
        pipe = control.pipeline
        self.assertEqual(pipe.budget["reserved_dimensions"], (640, 360))
        self.assertEqual(pipe.budget["video_frame_bytes"], frame_buffer_bytes(640, 360))
        self.assertEqual(pipe.budget["decoded_slots"], 24)
        self.assertEqual(pipe.step(0)["simulated_ring_capacity_bytes"],
                         assumed_ring_capacity_bytes(self.profile.free_xdr_mib, False))

    def test_ring_capacities_are_packet_aligned_and_capped(self):
        for live in (False, True):
            with self.subTest(live=live):
                ceiling = LIVE_RING_MAX if live else RING_MAX
                low = assumed_ring_capacity_bytes(1, live)
                high = assumed_ring_capacity_bytes(4096, live)
                self.assertEqual(low, RING_MIN // TS_PACKET_SIZE * TS_PACKET_SIZE)
                self.assertEqual(high, ceiling // TS_PACKET_SIZE * TS_PACKET_SIZE)
                self.assertEqual(low % TS_PACKET_SIZE, 0)
                self.assertEqual(high % TS_PACKET_SIZE, 0)

    def test_fourteen_mib_post_frame_free_xdr_is_not_unconditional_vod_max(self):
        free_mib = 14
        expected = (free_mib * 1024 * 1024 // 4 * 3 // TS_PACKET_SIZE
                    * TS_PACKET_SIZE)
        budget = StreamController(lambda *_: None)
        budget.configure(self.url("1080p", hd=True), {}, self.profile, live=False)
        self.assertEqual(budget.pipeline.budget["assumed_post_frame_free_xdr_mib"],
                         free_mib)
        self.assertEqual(budget.pipeline.budget["assumed_vod_ring_capacity_bytes"],
                         expected)
        self.assertLess(expected, RING_MAX)
        self.assertEqual(budget.pipeline.step(0)["simulated_ring_capacity_bytes"],
                         expected)

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

    def test_live_sample_keeps_requested_dimensions_separate_from_reserved(self):
        events = []
        control = StreamController(lambda name, values: events.append((name, values)))
        control.configure(self.url("360p"), {}, self.profile, live=True)
        control.ingest(bytes([0x47, 0x1f, 0xff, 0x10]) + bytes(184))
        sample = next(v for n, v in events if n == "sample")
        self.assertEqual(sample["simulated_requested_width"], 640)
        self.assertEqual(sample["simulated_requested_height"], 360)
        self.assertEqual(sample["simulated_reserved_width"], 1920)
        self.assertEqual(sample["simulated_reserved_height"], 1080)
        self.assertTrue(sample["simulated_live"])


if __name__ == "__main__":
    unittest.main()
