"""Timestamp measurement, unwrap, discontinuity and evidence contract tests.

All fixtures are synthetic 188-byte MPEG-TS packets assembled in-process.
No Jellyfin server, mpv process, hardware or real media is used.
"""
from datetime import datetime, timezone
from dataclasses import replace
import json
import unittest
from unittest import mock

from ps3lab.evidence import sanitize_evidence
from ps3lab.model import (
    Profile, Ps3Pipeline, TsInspector,
    PCR_GAP_TICKS, TS_CLOCK_HZ, TS_CLOCK_WRAP, av_sync_ms,
)
from ps3lab.player import JELLYFIN_TICKS_PER_SECOND, mpv_seconds_to_ticks
from ps3lab.proxy import StreamController


def _header(pid, cc, pusi=False, adaptation_payload=0x10, tei=False,
            scrambled=False):
    return bytearray([
        0x47,
        ((pid >> 8) & 0x1f) | (0x40 if pusi else 0) | (0x80 if tei else 0),
        pid & 0xff,
        adaptation_payload | (cc & 15) | (0xc0 if scrambled else 0),
    ])


def payload_packet(pid, cc, payload=b"", pusi=False, **flags):
    packet = _header(pid, cc, pusi, **flags)
    packet.extend(payload[:184])
    packet.extend(b"\xff" * (188 - len(packet)))
    return bytes(packet)


def pcr_packet(pid, cc, ticks, discontinuity=False):
    """Adaptation-only packet carrying a 33-bit 90 kHz PCR base."""
    packet = _header(pid, cc, adaptation_payload=0x20)
    packet.append(183)
    packet.append(0x10 | (0x80 if discontinuity else 0))
    packet.extend((
        (ticks >> 25) & 0xff,
        (ticks >> 17) & 0xff,
        (ticks >> 9) & 0xff,
        (ticks >> 1) & 0xff,
        ((ticks & 1) << 7) | 0x7e,
        0x00,
    ))
    packet.extend(b"\xff" * (188 - len(packet)))
    return bytes(packet)


def pts_packet(pid, cc, pts, stream_id=0xe0):
    data = bytearray(184)
    data[0:3] = b"\x00\x00\x01"
    data[3] = stream_id
    data[6] = 0x80
    data[7] = 0x80
    data[8] = 5
    data[9] = 0x21 | (((pts >> 30) & 0x07) << 1)
    data[10] = (pts >> 22) & 0xff
    data[11] = 0x01 | (((pts >> 15) & 0x7f) << 1)
    data[12] = (pts >> 7) & 0xff
    data[13] = 0x01 | ((pts & 0x7f) << 1)
    return payload_packet(pid, cc, data, pusi=True)


VIDEO_PID = 0x101
AUDIO_PID = 0x102
PCR_PID = 0x100


class TimestampUnitTests(unittest.TestCase):
    def test_transport_and_mpv_clock_units_are_distinct(self):
        self.assertEqual(TS_CLOCK_HZ, 90000)
        self.assertEqual(TS_CLOCK_WRAP, 1 << 33)
        self.assertEqual(JELLYFIN_TICKS_PER_SECOND, 10_000_000)
        self.assertAlmostEqual(av_sync_ms(90000, 0), 1000.0, places=2)
        self.assertAlmostEqual(av_sync_ms(0, 90000), -1000.0, places=2)
        self.assertEqual(mpv_seconds_to_ticks(1.5), 15_000_000)

    def test_pcr_span_is_converted_from_90khz_ticks_to_milliseconds(self):
        tracker = TsInspector()
        tracker.feed(pcr_packet(PCR_PID, 0, 0))
        tracker.feed(pcr_packet(PCR_PID, 1, 90))
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["pcr_elapsed_ms"], 1)
        self.assertEqual(snapshot["transport_clock_hz"], 90000)

    def test_raw_pts_ticks_and_signed_ms_share_the_transport_clock(self):
        tracker = TsInspector()
        tracker.video_pid, tracker.audio_pid = VIDEO_PID, AUDIO_PID
        tracker.feed(pts_packet(VIDEO_PID, 0, 180000))
        tracker.feed(pts_packet(AUDIO_PID, 0, 90000, stream_id=0xc0))
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["video_pts_ticks"], 180000)
        self.assertEqual(snapshot["audio_pts_ticks"], 90000)
        self.assertAlmostEqual(snapshot["pts_video_minus_audio_ms"], 1000.0,
                               places=2)


class PtsPcrUnwrapTests(unittest.TestCase):
    def test_pcr_physical_wrap_keeps_the_gap_lost_at_the_33bit_boundary(self):
        # 1 s before wrap to 1 s after wrap is 2 s of real transport time.
        tracker = TsInspector()
        tracker.feed(pcr_packet(PCR_PID, 0, TS_CLOCK_WRAP - 90000))
        tracker.feed(pcr_packet(PCR_PID, 1, 90000))
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["pcr_elapsed_ms"], 2000)
        self.assertEqual(snapshot["discontinuity_flags"], 0)

    def test_pcr_gap_larger_than_threshold_is_not_counted_as_continuity(self):
        tracker = TsInspector()
        tracker.feed(pcr_packet(PCR_PID, 0, 0))
        tracker.feed(pcr_packet(PCR_PID, 1, PCR_GAP_TICKS * 2))
        self.assertEqual(tracker.snapshot()["pcr_elapsed_ms"], 0)
        tracker.feed(pcr_packet(PCR_PID, 2, PCR_GAP_TICKS * 2 + 90000))
        self.assertEqual(tracker.snapshot()["pcr_elapsed_ms"], 1000)

    def test_pts_signed_delta_unwraps_in_both_directions(self):
        tracker = TsInspector()
        tracker.video_pid, tracker.audio_pid = VIDEO_PID, AUDIO_PID
        tracker.feed(pts_packet(VIDEO_PID, 0, 100))
        tracker.feed(pts_packet(AUDIO_PID, 0, TS_CLOCK_WRAP - 100,
                                stream_id=0xc0))
        self.assertAlmostEqual(
            tracker.snapshot()["pts_video_minus_audio_ms"], 2.22, places=2)

        tracker = TsInspector()
        tracker.video_pid, tracker.audio_pid = VIDEO_PID, AUDIO_PID
        tracker.feed(pts_packet(VIDEO_PID, 0, TS_CLOCK_WRAP - 100))
        tracker.feed(pts_packet(AUDIO_PID, 0, 100, stream_id=0xc0))
        self.assertAlmostEqual(
            tracker.snapshot()["pts_video_minus_audio_ms"], -2.22, places=2)

    def test_wrap_never_reports_a_multi_hour_offset(self):
        for video, audio in ((100, TS_CLOCK_WRAP - 100),
                             (TS_CLOCK_WRAP - 100, 100)):
            with self.subTest(video=video, audio=audio):
                self.assertLess(abs(av_sync_ms(video, audio)), 100)


class DiscontinuityClassificationTests(unittest.TestCase):
    def test_explicit_discontinuity_is_counted_but_excluded_from_span(self):
        tracker = TsInspector()
        tracker.feed(pcr_packet(PCR_PID, 0, 0))
        tracker.feed(pcr_packet(PCR_PID, 1, 90000))
        self.assertEqual(tracker.snapshot()["pcr_elapsed_ms"], 1000)
        tracker.feed(pcr_packet(PCR_PID, 2, 200000, discontinuity=True))
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["discontinuity_flags"], 1)
        self.assertEqual(snapshot["pcr_elapsed_ms"], 1000)
        tracker.feed(pcr_packet(PCR_PID, 3, 290000))
        self.assertEqual(tracker.snapshot()["pcr_elapsed_ms"], 2000)

    def test_continuity_errors_do_not_increment_discontinuity_counter(self):
        tracker = TsInspector()
        tracker.feed(payload_packet(0x120, 0))
        tracker.feed(payload_packet(0x120, 1))
        tracker.feed(payload_packet(0x120, 5))
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["continuity_errors"], 1)
        self.assertEqual(snapshot["discontinuity_flags"], 0)

    def test_transport_error_sync_loss_scramble_and_null_are_separate(self):
        tracker = TsInspector()
        tracker.feed(payload_packet(0x130, 0, tei=True))
        tracker.feed(b"\x00\x00" + payload_packet(0x131, 0))
        tracker.feed(payload_packet(0x132, 0, scrambled=True))
        tracker.feed(payload_packet(0x1fff, 0))
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["transport_errors"], 1)
        self.assertEqual(snapshot["sync_skipped_bytes"], 2)
        self.assertEqual(snapshot["scrambled_packets"], 1)
        self.assertEqual(snapshot["null_packets"], 1)
        self.assertEqual(snapshot["continuity_errors"], 0)
        self.assertEqual(snapshot["discontinuity_flags"], 0)


class AvSignedDistributionTests(unittest.TestCase):
    def test_signed_deltas_round_trip_across_a_range(self):
        for delta_ms in (-5000, -1000.5, -18.64, -0.01, 0.0, 0.01,
                         18.64, 1000.5, 5000):
            with self.subTest(delta_ms=delta_ms):
                audio = 1_000_000
                video = audio + round(delta_ms * TS_CLOCK_HZ / 1000.0)
                self.assertAlmostEqual(av_sync_ms(video, audio), delta_ms,
                                       delta=0.02)

    def test_sign_is_symmetric_outside_the_wrap_boundary(self):
        half = TS_CLOCK_WRAP >> 1
        self.assertGreater(av_sync_ms(half - 1, 0), 0)
        self.assertLess(av_sync_ms(half + 1, 0), 0)
        self.assertLess(av_sync_ms(0, half - 1), 0)
        self.assertGreater(av_sync_ms(0, half + 1), 0)

    def test_missing_pts_is_reported_as_missing_not_zero(self):
        self.assertIsNone(av_sync_ms(None, 0))
        self.assertIsNone(av_sync_ms(0, None))
        self.assertIsNone(av_sync_ms(None, None))


class DroppedFrameAndBufferMetricTests(unittest.TestCase):
    def profile(self):
        return replace(Profile.from_switches(False, False),
                       stream_video_bps=4000000)

    def test_ring_overflow_counts_modeled_dropped_frames_and_stays_bounded(self):
        engine = Ps3Pipeline(self.profile(), 1920, 1080, live=True)
        capacity = engine.budget["assumed_ring_capacity_bytes"]
        extra = 10 * 1024 * 1024
        result = engine.step(100, capacity + extra)
        self.assertEqual(result["simulated_ring_overflow_bytes"], extra)
        self.assertGreater(result["simulated_dropped_frames"], 0)
        self.assertGreaterEqual(result["simulated_ring_bytes"], 0)
        self.assertLessEqual(result["simulated_ring_bytes"], capacity)
        self.assertLessEqual(result["simulated_frame_queue"],
                             result["simulated_frame_slots"])
        self.assertIs(result["simulated_cpu_gpu_enabled"], False)

    def test_without_overflow_the_dropped_frame_metric_remains_zero(self):
        engine = Ps3Pipeline(self.profile(), 1920, 1080, live=True)
        result = engine.step(100, 1000)
        self.assertEqual(result["simulated_ring_overflow_bytes"], 0)
        self.assertEqual(result["simulated_ring_bytes"], 1000)
        self.assertEqual(result["simulated_dropped_frames"], 0)

    def test_dropped_frames_are_monotonic_and_never_negative(self):
        engine = Ps3Pipeline(self.profile(), 1280, 720, live=False)
        capacity = engine.budget["assumed_ring_capacity_bytes"]
        previous = 0
        for _ in range(5):
            result = engine.step(100, capacity + 1024 * 1024)
            self.assertGreaterEqual(result["simulated_dropped_frames"], previous)
            previous = result["simulated_dropped_frames"]
        self.assertGreater(previous, 0)


class MissingMeasurementSemanticsTests(unittest.TestCase):
    def test_empty_inspector_reports_missing_not_zero_for_optional_values(self):
        snapshot = TsInspector().snapshot()
        self.assertIsNone(snapshot["pcr_elapsed_ms"])
        self.assertIsNone(snapshot["video_pts_ticks"])
        self.assertIsNone(snapshot["audio_pts_ticks"])
        self.assertIsNone(snapshot["pts_video_minus_audio_ms"])
        self.assertEqual(snapshot["transport_clock_hz"], 90000)

    def test_one_pcr_is_a_measured_zero_span_not_missing(self):
        tracker = TsInspector()
        tracker.feed(pcr_packet(PCR_PID, 0, 45000))
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["pcr_elapsed_ms"], 0)
        self.assertIsNone(snapshot["pts_video_minus_audio_ms"])

    def test_video_pts_without_audio_is_missing_av_offset(self):
        tracker = TsInspector()
        tracker.video_pid = VIDEO_PID
        tracker.feed(pts_packet(VIDEO_PID, 0, 90000))
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["video_pts_ticks"], 90000)
        self.assertIsNone(snapshot["audio_pts_ticks"])
        self.assertIsNone(snapshot["pts_video_minus_audio_ms"])

    def test_transport_sample_keeps_mpv_and_physical_channels_missing(self):
        events = []
        control = StreamController(lambda name, values: events.append((name, values)))
        control.configure("http://localhost/fixture", {}, Profile.from_switches(False, False),
                          live=False)
        with mock.patch("ps3lab.proxy.time.monotonic", return_value=10.0):
            control.ingest(payload_packet(0x1fff, 0))
        sample = next(value for name, value in events if name == "sample")
        self.assertEqual(sample["transport_measurement_source"], "mpeg_ts_pts_pcr")
        self.assertEqual(sample["transport_clock_hz"], 90000)
        self.assertIsNone(sample["pcr_elapsed_ms"])
        self.assertIsNone(sample["mpv_clock_seconds"])
        self.assertIsNone(sample["physical_output_frames"])
        self.assertIs(sample["physical_output_measured"], False)

    def test_missing_or_invalid_mpv_clock_keeps_base_without_inventing_time(self):
        self.assertEqual(mpv_seconds_to_ticks(None, 500), 500)
        self.assertEqual(mpv_seconds_to_ticks(float("nan"), 500), 500)
        self.assertEqual(mpv_seconds_to_ticks(float("inf"), 500), 500)
        self.assertEqual(mpv_seconds_to_ticks("not-a-number", 500), 500)
        self.assertEqual(mpv_seconds_to_ticks(1.5, 500), 15_000_500)


class SanitizedStructuredEvidenceTests(unittest.TestCase):
    def test_sensitive_fields_urls_and_nonfinite_values_are_removed(self):
        record = sanitize_evidence("sample", {
            "token": "secret-token",
            "Authorization": "MediaBrowser Token=abc",
            "password": "hunter2",
            "url": "http://127.0.0.1/private",
            "path": "/media/private",
            "title": "Private Movie",
            "headers": {"Authorization": "abc"},
            "nested": {
                "pcr_elapsed_ms": 100,
                "video_pts_ticks": None,
                "value": float("nan"),
                "list": [1, "http://example/private", float("inf")],
            },
            "long": "x" * 200,
            "counter": 7,
            "flag": True,
        }, now=datetime(2026, 11, 24, tzinfo=timezone.utc))
        self.assertEqual(record["utc"], "2026-11-24T00:00:00+00:00")
        self.assertEqual(record["event"], "sample")
        data = record["data"]
        for key in ("token", "Authorization", "password", "url", "path",
                    "title", "headers"):
            self.assertNotIn(key, data)
        self.assertEqual(data["nested"]["pcr_elapsed_ms"], 100)
        self.assertIsNone(data["nested"]["video_pts_ticks"])
        self.assertIsNone(data["nested"]["value"])
        self.assertEqual(data["nested"]["list"], [1, "[redacted]", None])
        self.assertEqual(len(data["long"]), 92)
        self.assertEqual(data["counter"], 7)
        self.assertIs(data["flag"], True)
        json.dumps(record, allow_nan=False)

    def test_sample_record_is_strict_json_with_separated_channels(self):
        events = []
        control = StreamController(lambda name, values: events.append((name, values)))
        control.configure("http://localhost/fixture", {},
                          Profile.from_switches(False, False), live=False)
        with mock.patch("ps3lab.proxy.time.monotonic", return_value=10.0):
            control.ingest(payload_packet(0x1fff, 0))
        sample = next(value for name, value in events if name == "sample")
        record = sanitize_evidence("sample", sample,
                                   now=datetime(2026, 11, 24, tzinfo=timezone.utc))
        encoded = json.dumps(record, separators=(",", ":"), allow_nan=False)
        self.assertNotIn("http://", encoded)
        data = record["data"]
        self.assertEqual(data["transport_measurement_source"], "mpeg_ts_pts_pcr")
        self.assertIsNone(data["mpv_clock_seconds"])
        self.assertIsNone(data["physical_output_frames"])
        self.assertIs(data["physical_output_measured"], False)

    def test_recursion_is_bounded_and_unknown_objects_become_none(self):
        nested = current = {}
        for _ in range(10):
            current["child"] = {}
            current = current["child"]
        record = sanitize_evidence("bounded", {"nested": nested,
                                               "object": object()})
        self.assertIn("nested", record["data"])
        self.assertIsNone(record["data"]["object"])
        json.dumps(record, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
