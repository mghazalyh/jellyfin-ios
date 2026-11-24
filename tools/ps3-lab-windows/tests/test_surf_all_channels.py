"""Deterministic end-to-end fixtures for Surf All Channels on the desktop lab.

These tests use only synthetic MPIEG-TS bytes, luma samples and controller
events.  They do not start mpv, open a socket, contact Jellyfin/CatalogService
or decode real media.  A passing test is host-contract evidence, not physical
PS3 RSX/CellAudio evidence.
"""
import json
import unittest

from ps3lab.model import TsInspector
from ps3lab.surf_all_channels import (
    DESKTOP_CONTROLS, GATE_TIMEOUT_MS, SCHEMA, UNAVAILABLE_MEASUREMENTS,
    UNSUPPORTED_CONTROLS, AudioClock, PlaybackPosition,
    SurfAllChannelsHarness, TsFixtureBuilder, measurement_report,
    run_degraded_sequence, run_missing_controls_sequence,
    run_normal_sequence, signed_pts_delta,
)


class SyntheticTsFixtureTests(unittest.TestCase):
    def test_pat_pmt_pcr_and_pes_are_parsed_by_existing_inspector(self):
        builder = TsFixtureBuilder()
        data = (builder.pat() + builder.pmt() + builder.pcr(900000) +
                builder.video_pes(900000) + builder.audio_pes(899550))
        inspector = TsInspector()
        inspector.feed(data)
        snap = inspector.snapshot()
        self.assertEqual(snap["ts_packets"], 5)
        self.assertEqual(snap["pmt_pid"], 0x100)
        self.assertEqual(snap["video_pid"], 0x101)
        self.assertEqual(snap["audio_pid"], 0x102)
        self.assertEqual(snap["video_pes_starts"], 1)
        self.assertEqual(snap["audio_pes_starts"], 1)
        self.assertEqual(snap["pcr_elapsed_ms"], 0)
        self.assertAlmostEqual(snap["pts_video_minus_audio_ms"], 5.0,
                               places=2)
        self.assertEqual(snap["continuity_errors"], 0)
        self.assertEqual(snap["transport_errors"], 0)

    def test_discontinuity_flag_is_observable(self):
        builder = TsFixtureBuilder()
        inspector = TsInspector()
        inspector.feed(builder.pat() + builder.pmt() +
                       builder.pcr(900000) +
                       builder.video_pes(900000) +
                       builder.audio_pes(900000) +
                       builder.pcr(450000, discontinuity=True) +
                       builder.video_pes(1000) +
                       builder.audio_pes(500))
        snap = inspector.snapshot()
        self.assertEqual(snap["discontinuity_flags"], 1)
        self.assertEqual(inspector.video_pts, 1000)
        self.assertEqual(inspector.audio_pts, 500)

    def test_corruption_and_sync_loss_are_not_hidden(self):
        builder = TsFixtureBuilder()
        inspector = TsInspector()
        inspector.feed(builder.pat() + builder.pmt())
        inspector.feed(b"\x00\x00" + builder.corrupt_packet())
        snap = inspector.snapshot()
        self.assertGreaterEqual(snap["transport_errors"], 1)
        self.assertGreaterEqual(snap["sync_skipped_bytes"], 2)


class ClockAndPositionFixtureTests(unittest.TestCase):
    def test_signed_pts_delta_wraps_in_both_directions(self):
        self.assertEqual(signed_pts_delta(100, (1 << 33) - 100), 200)
        self.assertEqual(signed_pts_delta((1 << 33) - 100, 100), -200)
        self.assertEqual(signed_pts_delta(1206000, 1204322), 1678)
        self.assertIsNone(signed_pts_delta(None, 1))

    def test_audio_clock_reanchors_on_epoch_discontinuity_and_backward_pts(self):
        clock = AudioClock()
        first = clock.observe(900000, 900000, epoch=0)
        self.assertTrue(first["reset"])
        self.assertEqual(first["reason"], "epoch_change")
        running = clock.observe(990000, 990000, epoch=0)
        self.assertFalse(running["reset"])
        self.assertAlmostEqual(running["elapsed_ms"], 1000.0, places=3)
        backward = clock.observe(100000, 100000, epoch=0)
        self.assertTrue(backward["reset"])
        self.assertEqual(backward["reason"], "backward_reset")
        disc = clock.observe(1000, 500, epoch=0, discontinuity=True)
        self.assertTrue(disc["reset"])
        self.assertEqual(disc["reason"], "discontinuity")
        self.assertEqual(clock.reset_count, 3)

    def test_playback_position_never_negative_and_reanchors(self):
        position = PlaybackPosition()
        self.assertEqual(position.observe(0, epoch=0)["position_ms"], 0.0)
        self.assertEqual(position.observe(900000, epoch=0)["position_ms"],
                         10000.0)
        reset = position.observe(90000, epoch=1)
        self.assertTrue(reset["reset"])
        self.assertEqual(reset["reason"], "epoch_change")
        self.assertEqual(reset["position_ms"], 1000.0)
        self.assertTrue(position.monotonic)


class NormalSequenceTests(unittest.TestCase):
    def test_single_transition_releases_only_after_completed_flip(self):
        harness = run_normal_sequence(transitions=1)
        self.assertTrue(all(t.released for t in harness.transitions[1:]))
        self.assertEqual(harness.counters["timeouts"], 0)
        self.assertEqual(harness.counters["premature_releases"], 0)
        self.assertEqual(harness.counters["audio_releases"], 1)
        self.assertEqual(harness.counters["source_transitions"], 1)
        # Both decoded pictures occurred before the queued/completed flip.
        self.assertTrue(harness.transitions[1].scene_cut)
        self.assertEqual(
            harness.transitions[1].presentation_ack["sequence"], 2)
        self.assertEqual(harness.counters["audio_clock_resets"], 1)

    def test_hundred_deterministic_transitions_all_release(self):
        harness = run_normal_sequence(transitions=100)
        moved = harness.transitions[1:]
        self.assertEqual(len(moved), 100)
        self.assertEqual(harness.counters["source_transitions"], 100)
        self.assertEqual(harness.counters["audio_releases"], 100)
        self.assertEqual(harness.counters["timeouts"], 0)
        self.assertEqual(harness.counters["stale_flips"], 0)
        self.assertTrue(all(t.gate_started for t in moved))
        self.assertTrue(all(t.scene_cut for t in moved))
        self.assertTrue(all(t.released for t in moved))
        self.assertTrue(all(check["passed"]
                            for check in harness.contract_checks()))

    def test_queueing_a_flip_is_not_presentation_acknowledgement(self):
        harness = SurfAllChannelsHarness()
        harness.start(1, sync_enabled=True)
        harness.observe_source("a", "boot", "/live/all.ts")
        harness.observe_source("b", "boot", "/live/all.ts", at_ms=100)
        harness.tick(150)
        harness.decoded_picture(1, bytes(64), now_ms=200)
        harness.decoded_picture(2, bytes([250] * 64), now_ms=300)
        harness.queue_flip(2, now_ms=400)
        self.assertTrue(harness.gate.gating)
        self.assertEqual(harness.counters["audio_releases"], 0)
        self.assertEqual(harness.flip_complete(False, 2, now_ms=500),
                         "unchanged")
        self.assertTrue(harness.gate.gating)
        self.assertEqual(harness.counters["failed_flips"], 1)
        self.assertEqual(harness.flip_complete(True, 1, now_ms=600),
                         "unchanged")
        self.assertTrue(harness.gate.gating)
        self.assertEqual(harness.flip_complete(True, 2, now_ms=700),
                         "video_flip_audio_release")
        self.assertFalse(harness.gate.gating)


class DegradedSequenceTests(unittest.TestCase):
    def test_url_only_change_does_not_create_a_source_epoch(self):
        case = run_degraded_sequence()["url_only_change"]
        self.assertEqual(case.epoch, 0)
        self.assertEqual(case.counters["route_only_changes"], 1)
        self.assertEqual(len(case.transitions), 1)
        self.assertEqual(case.counters["source_transitions"], 0)

    def test_no_scene_cut_times_out_without_audio_release(self):
        case = run_degraded_sequence()["no_cut_timeout"]
        transition = case.transitions[-1]
        self.assertTrue(transition.timeout)
        self.assertEqual(transition.timeout_ms, GATE_TIMEOUT_MS + 151)
        self.assertFalse(transition.released)
        self.assertEqual(transition.release_reason, "gate_timeout_recovery")
        self.assertEqual(case.counters["audio_releases"], 0)
        self.assertFalse(case.gate.gating)

    def test_rapid_switch_stale_flip_cannot_release_new_gate(self):
        case = run_degraded_sequence()["rapid_switch_stale_flip"]
        self.assertEqual(case.counters["stale_flips"], 1)
        self.assertTrue(case.transitions[-1].stale_flip_ignored)
        # The old B candidate (sequence 2) did not release C; C's own
        # completed picture (sequence 4) did.
        self.assertEqual(case.counters["audio_releases"], 1)
        self.assertEqual(
            case.transitions[-1].presentation_ack["sequence"], 4)

    def test_paused_gate_does_not_count_paused_wall_time_until_resume(self):
        case = run_degraded_sequence()["pause_resume_timeout"]
        tick_events = [e for e in case.events if e["event"] == "gate_tick"]
        self.assertEqual(tick_events[0]["result"], "audio_gate_started")
        self.assertEqual(tick_events[1]["result"], "unchanged")
        self.assertEqual(tick_events[2]["result"], "gate_timeout_recovery")
        self.assertTrue(case.transitions[-1].timeout)

    def test_pcr_pts_discontinuity_resets_audio_clock_and_position(self):
        case = run_degraded_sequence()["pcr_pts_discontinuity"]
        self.assertGreaterEqual(case.counters["audio_clock_resets"], 2)
        self.assertGreaterEqual(case.counters["position_resets"], 2)
        self.assertEqual(case.inspector.snapshot()["discontinuity_flags"], 1)
        self.assertEqual(case.audio_clock.last_reset_reason, "discontinuity")
        self.assertTrue(case.position.monotonic)

    def test_corrupt_transport_is_reported_not_reinterpreted_as_decode(self):
        case = run_degraded_sequence()["corrupt_transport"]
        snap = case.inspector.snapshot()
        self.assertGreaterEqual(snap["transport_errors"], 1)
        self.assertGreaterEqual(snap["sync_skipped_bytes"], 2)
        self.assertFalse(case.snapshot()["gate"]["gating"])

    def test_disabled_sync_observes_source_without_starting_gate(self):
        case = run_degraded_sequence()["sync_disabled"]
        self.assertFalse(case.sync_enabled)
        self.assertEqual(case.counters["gate_starts"], 0)
        self.assertEqual(case.observations[-1]["event"],
                         "observed_sync_disabled")
        self.assertFalse(case.gate.gating)
        self.assertEqual(case.counters["audio_releases"], 0)

    def test_stale_generation_and_duplicate_controls_stay_bounded(self):
        harness = SurfAllChannelsHarness()
        harness.start(3, sync_enabled=True)
        self.assertEqual(harness.observe_source("a", "boot", generation=2),
                         "stale_generation")
        self.assertEqual(harness.counters["stale_observations"], 1)
        self.assertEqual(harness.control("pause", now_ms=100), "accepted")
        self.assertEqual(harness.control("pause", now_ms=200),
                         "duplicate_ignored")
        self.assertEqual(harness.counters["duplicate_controls"], 1)
        self.assertEqual(harness.control("seek", now_ms=300), "rejected_live")
        self.assertEqual(harness.control("stop", now_ms=400), "stopped")


class MissingControlsTests(unittest.TestCase):
    def test_every_console_only_control_is_explicitly_not_implemented(self):
        harness = run_missing_controls_sequence()
        statuses = {row["control"]: row for row in harness.controls}
        for name in UNSUPPORTED_CONTROLS:
            self.assertIn(name, statuses)
            self.assertEqual(statuses[name]["status"], "NOT IMPLEMENTED")
            self.assertFalse(statuses[name]["counted_as_tested"])
        self.assertEqual(harness.counters["not_implemented_controls"],
                         len(UNSUPPORTED_CONTROLS))
        self.assertEqual(statuses["seek"]["status"], "rejected_live")
        self.assertTrue(statuses["seek"]["counted_as_tested"])

    def test_desktop_control_matrix_is_explicit(self):
        self.assertEqual(set(DESKTOP_CONTROLS),
                         {"play", "pause", "resume", "stop", "toggle_sync",
                          "seek"})
        self.assertEqual(DESKTOP_CONTROLS["seek"]["desktop"],
                         "REJECTED_LIVE")
        self.assertNotIn("favorite_current", DESKTOP_CONTROLS)


class MeasurementTests(unittest.TestCase):
    def test_measurement_report_is_deterministic_and_json_safe(self):
        first = measurement_report(transitions=100)
        second = measurement_report(transitions=100)
        self.assertEqual(first, second)
        encoded = json.dumps(first, sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["schema"], SCHEMA)
        self.assertFalse(decoded["physical"])
        self.assertFalse(decoded["live_server_calls"])
        self.assertFalse(decoded["actual_media"])
        self.assertEqual(decoded["metric_source"],
                         "synthetic_modeled_fixture")
        self.assertEqual(decoded["totals"]["normal_transitions"], 101)
        self.assertEqual(decoded["totals"]["normal_releases"], 100)
        self.assertEqual(decoded["totals"]["normal_timeouts"], 0)
        self.assertGreaterEqual(decoded["totals"]["fixture_transitions"], 100)

    def test_unavailable_physical_measurements_are_null_not_zero(self):
        report = measurement_report(transitions=1)
        unavailable = report["capabilities"]["unavailable_measurements"]
        self.assertTrue(unavailable)
        for key, value in UNAVAILABLE_MEASUREMENTS.items():
            self.assertIn(key, unavailable)
            self.assertIsNone(unavailable[key])
            self.assertIsNone(value)
        self.assertEqual(report["actual_media"], False)
        self.assertEqual(report["physical"], False)
        inspector = report["normal"]["snapshot"]["inspector"]
        self.assertNotIn("physical_allocation_measured", inspector)

    def test_all_contract_checks_pass_for_normal_and_degraded_paths(self):
        report = measurement_report(transitions=100)
        normal_checks = report["normal"]["contract_checks"]
        self.assertTrue(normal_checks)
        self.assertTrue(all(check["passed"] for check in normal_checks))
        for name, case in report["degraded"].items():
            with self.subTest(case=name):
                self.assertTrue(case["contract_checks"])
                self.assertTrue(all(check["passed"]
                                    for check in case["contract_checks"]))


if __name__ == "__main__":
    unittest.main()
