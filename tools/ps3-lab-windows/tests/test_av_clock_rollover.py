"""Signed A/V PTS and clock-rollover matrix with synthetic MPEG-TS fixtures.

All media here is assembled byte-by-byte in memory. No real media is decoded,
no server is contacted and no hardware result is claimed. Expected values are
computed independently from the implementation under test.
"""
import random
import unittest

from ps3lab.model import TsInspector, av_sync_ms

WRAP = 1 << 33
HALF = 1 << 32
TICKS_PER_MS = 90

PMT_PID = 0x1000
VIDEO_PID = 0x0100
AUDIO_PID = 0x0101


def encode_timestamp(value, prefix=0b0010):
    """Encode a 33-bit PTS/DTS field exactly as ISO/IEC 13818-1 does."""
    return bytes([
        (prefix << 4) | (((value >> 30) & 0x7) << 1) | 1,
        (value >> 22) & 0xFF,
        (((value >> 15) & 0x7F) << 1) | 1,
        (value >> 7) & 0xFF,
        ((value & 0x7F) << 1) | 1,
    ])


def pes_packet(stream_id, pts=None, dts=None):
    flags = 0
    body = b""
    if pts is not None:
        flags |= 0x80
        body += encode_timestamp(pts, 0b0010)
    if dts is not None:
        flags |= 0x40
        body += encode_timestamp(dts, 0b0001)
    return (b"\x00\x00\x01" + bytes([stream_id]) + b"\x00\x00" +
            b"\x80" + bytes([flags, len(body)]) + body)


def ts_packet(pid, cc, payload=b"", pusi=False, pcr=None, discontinuity=False):
    if pcr is not None:
        af_flags = (0x80 if discontinuity else 0) | 0x10
        pcr_bytes = bytes([
            (pcr >> 25) & 0xFF, (pcr >> 17) & 0xFF, (pcr >> 9) & 0xFF,
            (pcr >> 1) & 0xFF, (pcr & 1) << 7, 0x00,
        ])
        if payload:
            adaptation = bytes([af_flags]) + pcr_bytes
            header_control = 0x30
            rest = payload
        else:
            adaptation = (bytes([af_flags]) + pcr_bytes +
                          b"\xff" * (183 - 7))
            header_control = 0x20
            rest = b""
        header = bytes([
            0x47, ((pid >> 8) & 0x1F) | (0x40 if pusi else 0), pid & 0xFF,
            header_control | (cc & 15),
        ])
        return (header + bytes([len(adaptation)]) + adaptation + rest)[:188].ljust(188, b"\xff")
    header = bytes([
        0x47, ((pid >> 8) & 0x1F) | (0x40 if pusi else 0), pid & 0xFF,
        0x10 | (cc & 15),
    ])
    return (header + payload)[:188].ljust(188, b"\xff")


def pat_packet():
    section = bytearray([
        0x00, 0xB0, 0x0D, 0x00, 0x01, 0xC1, 0x00, 0x00,
        0x00, 0x01, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF,
    ])
    section += b"\x00\x00\x00\x00"
    return ts_packet(0, 0, b"\x00" + bytes(section), pusi=True)


def pmt_packet():
    es = bytes([0x1B, 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0x00])
    es += bytes([0x03, 0xE0 | (AUDIO_PID >> 8), AUDIO_PID & 0xFF, 0xF0, 0x00])
    body = bytearray([0x00, 0x01, 0xC1, 0x00, 0x00,
                      0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0x00])
    section = bytearray([0x02, 0xB0, 0x00]) + body + es
    length = 9 + len(es) + 4
    section[1] = 0xB0 | (length >> 8)
    section[2] = length & 0xFF
    section += b"\x00\x00\x00\x00"
    return ts_packet(PMT_PID, 0, b"\x00" + bytes(section), pusi=True)


def tables():
    return pat_packet() + pmt_packet()


def expected_av_ms(video_pts, audio_pts):
    """Independent reference: unsigned modulo, then move to [-half, half)."""
    if video_pts is None or audio_pts is None:
        return None
    delta = (video_pts - audio_pts) % WRAP
    if delta >= HALF:
        delta -= WRAP
    return round(delta / 90.0, 2)


class AVSyncArithmeticTests(unittest.TestCase):
    def test_known_signed_offsets_and_both_wrap_directions(self):
        cases = (
            # Reproduced from a real H.264/MP3 transport (commit context).
            (1204322, 1206000, -18.64),
            (1206000, 1204322, 18.64),
            # Video just after the 33-bit rollover, audio just before it.
            (100, WRAP - 100, 2.22),
            # Audio just after the rollover, video just before it.
            (WRAP - 100, 100, -2.22),
            (5, WRAP - 5, 0.11),
            (WRAP - 5, 5, -0.11),
            (0, 0, 0.0),
            (12345, 12345, 0.0),
        )
        for video_pts, audio_pts, expected in cases:
            with self.subTest(video_pts=video_pts, audio_pts=audio_pts):
                self.assertAlmostEqual(
                    av_sync_ms(video_pts, audio_pts), expected, places=2)

    def test_zero_pts_is_a_timestamp_and_missing_is_none(self):
        self.assertEqual(av_sync_ms(0, 0), 0.0)
        self.assertEqual(av_sync_ms(0, 900), -10.0)
        self.assertEqual(av_sync_ms(900, 0), 10.0)
        self.assertIsNone(av_sync_ms(None, 0))
        self.assertIsNone(av_sync_ms(0, None))
        self.assertIsNone(av_sync_ms(None, None))

    def test_matrix_matches_independent_modular_arithmetic(self):
        boundary = (0, 1, TICKS_PER_MS, HALF - 1, HALF, HALF + 1,
                    WRAP - 2, WRAP - 1)
        pairs = [(video, audio) for video in boundary for audio in boundary]
        rng = random.Random(20261124)
        for _ in range(500):
            pairs.append((rng.randrange(WRAP), rng.randrange(WRAP)))
        # Exercise small signed offsets in both directions at many bases.
        for base in (0, 90000, HALF - 2000, HALF, WRAP - 200000, WRAP - 1):
            for delta in (-100000, -1678, -1, 0, 1, 1678, 100000):
                pairs.append((base, (base - delta) % WRAP))
        for video_pts, audio_pts in pairs:
            with self.subTest(video_pts=video_pts, audio_pts=audio_pts):
                self.assertEqual(
                    av_sync_ms(video_pts, audio_pts),
                    expected_av_ms(video_pts, audio_pts))

    def test_half_wrap_boundary_uses_half_open_interval(self):
        # The nearest modular difference is [-2**32, 2**32); both antipodes
        # resolve to -2**32, and the value just below +2**32 stays positive.
        self.assertEqual(av_sync_ms(HALF, 0), round(-HALF / 90.0, 2))
        self.assertEqual(av_sync_ms(0, HALF), round(-HALF / 90.0, 2))
        self.assertEqual(av_sync_ms(HALF - 1, 0), round((HALF - 1) / 90.0, 2))
        self.assertEqual(av_sync_ms(0, HALF - 1), round(-(HALF - 1) / 90.0, 2))
        # One tick short of a full revolution is nearest to -1 tick, not to
        # a +26.5 hour difference.
        self.assertEqual(av_sync_ms(WRAP - 1, 0), -0.01)
        self.assertEqual(av_sync_ms(0, WRAP - 1), 0.01)


class SyntheticTransportFixtureTests(unittest.TestCase):
    def inspector(self):
        tracker = TsInspector()
        tracker.feed(tables())
        return tracker

    def test_small_negative_offset_end_to_end(self):
        tracker = self.inspector()
        tracker.feed(ts_packet(VIDEO_PID, 0,
                               pes_packet(0xE0, pts=1204322), pusi=True))
        tracker.feed(ts_packet(AUDIO_PID, 0,
                               pes_packet(0xC0, pts=1206000), pusi=True))
        snap = tracker.snapshot()
        self.assertEqual(snap["video_pid"], VIDEO_PID)
        self.assertEqual(snap["audio_pid"], AUDIO_PID)
        self.assertEqual(snap["video_pes_starts"], 1)
        self.assertEqual(snap["audio_pes_starts"], 1)
        self.assertEqual(snap["pts_video_minus_audio_ms"], -18.64)

    def test_both_wrap_directions_end_to_end(self):
        video_after = self.inspector()
        video_after.feed(ts_packet(VIDEO_PID, 0,
                                   pes_packet(0xE0, pts=100), pusi=True))
        video_after.feed(ts_packet(AUDIO_PID, 0,
                                   pes_packet(0xC0, pts=WRAP - 100), pusi=True))
        self.assertEqual(
            video_after.snapshot()["pts_video_minus_audio_ms"], 2.22)

        audio_after = self.inspector()
        audio_after.feed(ts_packet(VIDEO_PID, 0,
                                   pes_packet(0xE0, pts=WRAP - 100), pusi=True))
        audio_after.feed(ts_packet(AUDIO_PID, 0,
                                   pes_packet(0xC0, pts=100), pusi=True))
        self.assertEqual(
            audio_after.snapshot()["pts_video_minus_audio_ms"], -2.22)

    def test_pts_zero_is_reported_but_missing_pts_is_none(self):
        zero = self.inspector()
        zero.feed(ts_packet(VIDEO_PID, 0,
                            pes_packet(0xE0, pts=0), pusi=True))
        zero.feed(ts_packet(AUDIO_PID, 0,
                            pes_packet(0xC0, pts=0), pusi=True))
        self.assertEqual(zero.snapshot()["pts_video_minus_audio_ms"], 0.0)

        asymmetric = self.inspector()
        asymmetric.feed(ts_packet(VIDEO_PID, 0,
                                  pes_packet(0xE0, pts=900), pusi=True))
        asymmetric.feed(ts_packet(AUDIO_PID, 0,
                                  pes_packet(0xC0, pts=0), pusi=True))
        self.assertEqual(
            asymmetric.snapshot()["pts_video_minus_audio_ms"], 10.0)

        missing_audio = self.inspector()
        missing_audio.feed(ts_packet(VIDEO_PID, 0,
                                     pes_packet(0xE0, pts=900), pusi=True))
        self.assertIsNone(
            missing_audio.snapshot()["pts_video_minus_audio_ms"])

        no_pts_field = self.inspector()
        no_pts_field.feed(ts_packet(VIDEO_PID, 0,
                                    pes_packet(0xE0, pts=900), pusi=True))
        no_pts_field.feed(ts_packet(AUDIO_PID, 0,
                                    pes_packet(0xC0), pusi=True))
        self.assertIsNone(no_pts_field.snapshot()["pts_video_minus_audio_ms"])

    def test_dts_only_is_missing_but_pes_with_pts_and_dts_uses_pts(self):
        # A PES start that carries only DTS must not be mistaken for a PTS.
        dts_only = self.inspector()
        dts_only.feed(ts_packet(VIDEO_PID, 0,
                                pes_packet(0xE0, pts=9000), pusi=True))
        dts_only.feed(ts_packet(AUDIO_PID, 0,
                                pes_packet(0xC0, dts=8000), pusi=True))
        self.assertIsNone(dts_only.snapshot()["pts_video_minus_audio_ms"])

        # B-frame decode order with reordered PTS and a DTS on every PES.
        b_frames = self.inspector()
        for index, (pts, dts) in enumerate(((3000, 0), (1000, 900), (2000, 1800))):
            b_frames.feed(ts_packet(VIDEO_PID, index,
                                    pes_packet(0xE0, pts=pts, dts=dts),
                                    pusi=True))
        b_frames.feed(ts_packet(AUDIO_PID, 0,
                                pes_packet(0xC0, pts=1000), pusi=True))
        snap = b_frames.snapshot()
        # Last decoded video PTS is 2000; reading DTS (1800) would be 8.89 ms.
        self.assertEqual(snap["pts_video_minus_audio_ms"], 11.11)

    def test_per_stream_clock_reset_stays_bounded(self):
        # A video timeline reset from just before the rollover to zero is
        # indistinguishable from a forward rollover using PTS alone. It must
        # remain a small, bounded observation rather than a 13.2 hour value.
        reset_video = self.inspector()
        reset_video.feed(ts_packet(VIDEO_PID, 0,
                                   pes_packet(0xE0, pts=WRAP - 90000), pusi=True))
        reset_video.feed(ts_packet(AUDIO_PID, 0,
                                   pes_packet(0xC0, pts=WRAP - 900), pusi=True))
        reset_video.feed(ts_packet(VIDEO_PID, 1,
                                   pes_packet(0xE0, pts=0), pusi=True))
        self.assertEqual(
            reset_video.snapshot()["pts_video_minus_audio_ms"], 10.0)

        reset_audio = self.inspector()
        reset_audio.feed(ts_packet(VIDEO_PID, 0,
                                   pes_packet(0xE0, pts=WRAP - 900), pusi=True))
        reset_audio.feed(ts_packet(AUDIO_PID, 0,
                                   pes_packet(0xC0, pts=WRAP - 90000), pusi=True))
        reset_audio.feed(ts_packet(AUDIO_PID, 1,
                                   pes_packet(0xC0, pts=0), pusi=True))
        self.assertEqual(
            reset_audio.snapshot()["pts_video_minus_audio_ms"], -10.0)

    def test_pts_offset_is_observation_not_rendered_lip_sync(self):
        tracker = self.inspector()
        tracker.feed(ts_packet(VIDEO_PID, 0,
                               pes_packet(0xE0, pts=1204322), pusi=True))
        tracker.feed(ts_packet(AUDIO_PID, 0,
                               pes_packet(0xC0, pts=1206000), pusi=True))
        snap = tracker.snapshot()
        self.assertEqual(snap["pts_video_minus_audio_ms"], -18.64)
        # The observation is explicitly a PTS offset; no rendered/lip-sync
        # result is derived from transport timestamps by this observer.
        self.assertFalse(
            [key for key in snap if "lip" in key or "render" in key])

    def test_fixture_is_deterministic_when_split_across_chunks(self):
        stream = tables()
        stream += ts_packet(VIDEO_PID, 0,
                            pes_packet(0xE0, pts=100), pusi=True)
        stream += ts_packet(AUDIO_PID, 0,
                            pes_packet(0xC0, pts=WRAP - 100), pusi=True)
        whole = TsInspector()
        whole.feed(stream)
        split = TsInspector()
        for offset in range(0, len(stream), 7):
            split.feed(stream[offset:offset + 7])
        self.assertEqual(whole.snapshot(), split.snapshot())
        self.assertEqual(
            split.snapshot()["pts_video_minus_audio_ms"], 2.22)


class PcrClockRolloverTests(unittest.TestCase):
    def inspector(self):
        tracker = TsInspector()
        tracker.feed(tables())
        return tracker

    def feed_pcr(self, tracker, values):
        for index, item in enumerate(values):
            if isinstance(item, tuple):
                pcr, discontinuity = item
            else:
                pcr, discontinuity = item, False
            tracker.feed(ts_packet(VIDEO_PID, index, pcr=pcr,
                                   discontinuity=discontinuity))

    def test_monotonic_pcr_elapsed(self):
        tracker = self.inspector()
        self.feed_pcr(tracker, [90000, 180000, 270000])
        snap = tracker.snapshot()
        self.assertEqual(snap["pcr_elapsed_ms"], 2000)
        self.assertGreaterEqual(snap["pcr_elapsed_ms"], 0)

    def test_explicit_discontinuity_and_backward_reset_stay_nonnegative(self):
        explicit = self.inspector()
        self.feed_pcr(explicit, [
            (90000, False), (180000, False), (270000, True), (360000, False),
        ])
        # Contiguous observed segments: 90k..180k and 270k..360k = 1000+1000.
        self.assertEqual(explicit.snapshot()["pcr_elapsed_ms"], 2000)

        backward = self.inspector()
        self.feed_pcr(backward, [90000, 180000, 50000, 140000])
        # Backward step resets the segment: 90k..180k and 50k..140k.
        self.assertEqual(backward.snapshot()["pcr_elapsed_ms"], 2000)
        self.assertEqual(backward.snapshot()["discontinuity_flags"], 0)

    def test_forward_33bit_pcr_rollover_is_progress_not_reset(self):
        tracker = self.inspector()
        self.feed_pcr(tracker, [WRAP - 180000, WRAP - 90000, 90000, 180000])
        # True modular span: 180000 ticks to the rollover plus 180000 after
        # = 360000 ticks / 90 = 4000 ms. A reset would under-report 2000 ms.
        self.assertEqual(tracker.snapshot()["pcr_elapsed_ms"], 4000)
        self.assertEqual(tracker.snapshot()["discontinuity_flags"], 0)

    def test_large_forward_gap_is_treated_as_a_new_segment(self):
        tracker = self.inspector()
        self.feed_pcr(tracker, [90000, 1000000, 1090000])
        # 900000 ticks is the 10 second segment threshold; the first pair is
        # a new segment, and only 1090000-1000000 = 90000 ticks is counted.
        self.assertEqual(tracker.snapshot()["pcr_elapsed_ms"], 1000)


if __name__ == "__main__":
    unittest.main()
