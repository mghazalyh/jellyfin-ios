"""Deterministic coverage of the corrected native Surf PCM gate contract.

Reference: physical-ps3-ring-fix/source/player/surf_sync.cpp and
surf_gate_logic.h at commit e7c1cce9b10c162c5d94d431b12895735b1c78e4,
plus tests/test_surf_sync.cpp in that commit.

These are host-only synthetic sequences. They do not run PS3 code, decode
media, touch the network, or prove physical RSX/audio presentation.
"""
import unittest

from ps3lab.sourcegate import SurfAudioGate, is_scene_cut, sequence_reached


BLACK = bytes(64)
WHITE = bytes([240] * 64)
CUT = bytes([34] * 64)


def started_gate(source="source-a", boot="boot-1", generation=7):
    gate = SurfAudioGate()
    gate.session(generation)
    gate.set_enabled(True)
    gate.observe(generation, source, boot)
    return gate


def gated_after_change(generation=7, now_ms=1000):
    gate = started_gate(generation=generation)
    gate.observe(generation, "source-b", "boot-1")
    assert gate.tick(now_ms) == "audio_gate_started"
    assert gate.decoded_picture(1, BLACK) == "unchanged"
    assert gate.decoded_picture(2, CUT) == "visual_cut_decoded"
    return gate


class NativeFlipQualificationTests(unittest.TestCase):
    def test_superseded_source_invalidates_a_queued_flip(self):
        gate = gated_after_change()
        gate.flip_queued(2)
        self.assertTrue(gate.flip_qualified)
        self.assertEqual(gate.observe(7, "source-c", "boot-1"),
                         "transition_observed")
        self.assertFalse(gate.flip_qualified)
        self.assertFalse(gate.cut_found)
        # Pending transition also blocks decoding a new candidate.
        self.assertEqual(gate.decoded_picture(3, BLACK), "unchanged")
        self.assertEqual(gate.flip_complete(True), "unchanged")
        self.assertTrue(gate.gating)

        self.assertEqual(gate.tick(1001), "audio_gate_started")
        gate.decoded_picture(4, BLACK)
        self.assertEqual(gate.decoded_picture(5, CUT), "visual_cut_decoded")
        # A completion without a newly qualified queued flip is not evidence.
        self.assertEqual(gate.flip_complete(True), "unchanged")
        self.assertTrue(gate.gating)
        gate.flip_queued(5)
        self.assertTrue(gate.flip_qualified)
        self.assertEqual(gate.flip_complete(True),
                         "video_flip_audio_release")
        self.assertFalse(gate.gating)

    def test_failed_completion_consumes_qualification_once(self):
        gate = gated_after_change()
        gate.flip_queued(2)
        self.assertEqual(gate.flip_complete(False), "unchanged")
        self.assertFalse(gate.flip_qualified)
        self.assertTrue(gate.gating)
        # A later successful completion without a new queue cannot release.
        self.assertEqual(gate.flip_complete(True), "unchanged")
        self.assertTrue(gate.gating)
        gate.flip_queued(2)
        self.assertEqual(gate.flip_complete(True),
                         "video_flip_audio_release")

    def test_unreached_or_pending_queue_is_not_qualified(self):
        gate = gated_after_change()
        gate.flip_queued(1)
        self.assertFalse(gate.flip_qualified)
        self.assertEqual(gate.flip_complete(True), "unchanged")
        self.assertTrue(gate.gating)

        gate.flip_queued(2)
        self.assertTrue(gate.flip_qualified)
        gate.observe(7, "source-c", "boot-1")
        gate.flip_queued(2)
        self.assertFalse(gate.flip_qualified)
        self.assertEqual(gate.tick(1001), "audio_gate_started")
        self.assertEqual(gate.flip_complete(True), "unchanged")
        self.assertTrue(gate.gating)

    def test_duplicate_enable_keeps_a_qualified_flip(self):
        gate = gated_after_change()
        gate.flip_queued(2)
        gate.set_enabled(True)
        self.assertTrue(gate.flip_qualified)
        self.assertTrue(gate.gating)
        self.assertEqual(gate.flip_complete(True),
                         "video_flip_audio_release")

    def test_stale_generation_does_not_invalidate_a_live_qualification(self):
        gate = gated_after_change()
        gate.flip_queued(2)
        self.assertEqual(gate.observe(8, "source-stale", "boot-1"), "ignored")
        gate.set_enabled(True)
        self.assertTrue(gate.flip_qualified)
        self.assertEqual(gate.flip_complete(True),
                         "video_flip_audio_release")

    def test_disabling_or_stopping_clears_qualification(self):
        gate = gated_after_change()
        gate.flip_queued(2)
        gate.set_enabled(False)
        self.assertFalse(gate.flip_qualified)
        self.assertEqual(gate.flip_complete(True), "unchanged")
        self.assertEqual(gate.event, "sync_beta_disabled")

        gate.set_enabled(True)
        gate.observe(7, "source-c", "boot-1")
        self.assertEqual(gate.tick(2000), "audio_gate_started")
        gate.decoded_picture(3, BLACK)
        gate.decoded_picture(4, CUT)
        gate.flip_queued(4)
        gate.stop()
        self.assertFalse(gate.flip_qualified)
        self.assertEqual(gate.flip_complete(True), "unchanged")
        self.assertEqual(gate.observe(7, "source-d", "boot-1"), "ignored")


class NativeContinuousMuteTests(unittest.TestCase):
    def test_repeated_rotations_do_not_extend_the_mute_ceiling(self):
        gate = gated_after_change(now_ms=1000)
        self.assertEqual(gate.gate_started_ms, 1000)
        gate.observe(7, "source-c", "boot-1")
        self.assertEqual(gate.tick(2000), "audio_gate_started")
        self.assertEqual(gate.gate_started_ms, 1000)
        gate.observe(7, "source-d", "boot-1")
        self.assertEqual(gate.tick(3500), "audio_gate_started")
        self.assertEqual(gate.gate_started_ms, 1000)
        self.assertEqual(gate.tick(3999), "unchanged")
        self.assertEqual(gate.tick(4000), "gate_timeout_recovery")
        self.assertFalse(gate.gating)
        self.assertFalse(gate.flip_qualified)

    def test_fresh_gate_after_release_gets_a_new_ceiling(self):
        gate = gated_after_change(now_ms=1000)
        self.assertEqual(gate.tick(4000), "gate_timeout_recovery")
        gate.observe(7, "source-c", "boot-1")
        self.assertEqual(gate.tick(5000), "audio_gate_started")
        self.assertEqual(gate.gate_started_ms, 5000)
        self.assertEqual(gate.tick(7999), "unchanged")
        self.assertEqual(gate.tick(8000), "gate_timeout_recovery")

    def test_timeout_invalidates_a_qualified_flip(self):
        gate = gated_after_change(now_ms=1000)
        gate.flip_queued(2)
        self.assertTrue(gate.flip_qualified)
        self.assertEqual(gate.tick(4000), "gate_timeout_recovery")
        self.assertFalse(gate.flip_qualified)
        self.assertEqual(gate.flip_complete(True), "unchanged")
        self.assertFalse(gate.gating)

    def test_passthrough_skips_and_clears_the_gate(self):
        gate = started_gate()
        gate.observe(7, "source-b", "boot-1")
        self.assertEqual(gate.tick(1000, passthrough=True),
                         "bitstream_gate_skipped")
        self.assertFalse(gate.gating)
        self.assertEqual(gate.decoded_picture(1, BLACK), "unchanged")
        self.assertEqual(gate.decoded_picture(2, CUT), "unchanged")
        gate.flip_queued(2)
        self.assertFalse(gate.flip_qualified)
        self.assertEqual(gate.flip_complete(True), "unchanged")

    def test_pause_holds_pending_and_blocks_timeout(self):
        gate = started_gate()
        gate.observe(7, "source-b", "boot-1")
        self.assertEqual(gate.tick(1000, paused=True), "unchanged")
        self.assertTrue(gate.transition_pending)
        self.assertEqual(gate.tick(1001), "audio_gate_started")
        self.assertEqual(gate.tick(9000, paused=True), "unchanged")
        self.assertTrue(gate.gating)
        self.assertEqual(gate.tick(9001), "gate_timeout_recovery")


class NativeCutAndSequenceTests(unittest.TestCase):
    def test_cut_event_is_reported_once_and_decode_alone_never_releases(self):
        gate = gated_after_change()
        self.assertEqual(gate.decoded_picture(2, CUT), "unchanged")
        self.assertEqual(gate.decoded_picture(3, WHITE), "unchanged")
        self.assertEqual(gate.candidate_seq, 2)
        self.assertTrue(gate.gating)
        self.assertTrue(is_scene_cut(BLACK, CUT)[0])

    def test_sequence_zero_candidate_can_be_qualified(self):
        gate = started_gate()
        gate.observe(7, "source-b", "boot-1")
        self.assertEqual(gate.tick(1000), "audio_gate_started")
        gate.decoded_picture(0, BLACK)
        self.assertEqual(gate.decoded_picture(1, CUT), "visual_cut_decoded")
        self.assertEqual(gate.candidate_seq, 1)
        gate.flip_queued(0)
        self.assertFalse(gate.flip_qualified)
        gate.flip_queued(1)
        self.assertTrue(gate.flip_qualified)
        self.assertEqual(gate.flip_complete(True),
                         "video_flip_audio_release")

    def test_32_bit_rollover_candidate_and_zero_presentation(self):
        self.assertTrue(sequence_reached(0, 0xffffffff))
        self.assertTrue(sequence_reached(1, 0xffffffff))
        self.assertFalse(sequence_reached(0xfffffffe, 0xffffffff))

        gate = started_gate()
        gate.observe(7, "source-b", "boot-1")
        self.assertEqual(gate.tick(1000), "audio_gate_started")
        gate.decoded_picture(0xfffffffe, BLACK)
        self.assertEqual(gate.decoded_picture(0xffffffff, CUT),
                         "visual_cut_decoded")
        self.assertEqual(gate.candidate_seq, 0xffffffff)
        gate.flip_queued(0)
        self.assertTrue(gate.flip_qualified)
        self.assertEqual(gate.flip_complete(True),
                         "video_flip_audio_release")

    def test_session_reset_clears_qualification_and_sequence(self):
        gate = gated_after_change()
        gate.flip_queued(2)
        gate.session(8)
        self.assertFalse(gate.flip_qualified)
        self.assertEqual(gate.flip_seq, 0)
        self.assertFalse(gate.gating)
        self.assertFalse(gate.enabled)
        self.assertEqual(gate.event, "session_started")
        self.assertEqual(gate.flip_complete(True), "unchanged")


if __name__ == "__main__":
    unittest.main()
