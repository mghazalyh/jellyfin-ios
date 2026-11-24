"""Bounded virtual-time endurance for the PS3 pipeline model.

All values here are deterministic model outputs. ``simulate_scenario`` never
touches a decoder, the network or a PS3, so a PASS is not a hardware result.
"""
import unittest
from dataclasses import replace

from ps3lab.model import (
    LIVE_RING_MAX, RING_MAX,
    RING_MIN, TS_PACKET_SIZE, NetworkShaper, Profile, Ps3Pipeline,
    assumed_ring_capacity_bytes, ps3_memory_budget, simulate_scenario,
)

FPS_VALUES = (23.976, 24, 25, 29.97, 30)
MODES = (False, True)
ENDURANCE_SECONDS = 1800
JITTER_HORIZON_SECONDS = 900
JITTER_CHUNK_BYTES = 188 * 64
JITTER_VALUES = (0.0, 30.0, 250.0)


def expected_ring_bytes(free_xdr_mib, live):
    return assumed_ring_capacity_bytes(free_xdr_mib, live=live)


def packet_aligned(value):
    return value // TS_PACKET_SIZE * TS_PACKET_SIZE


class VirtualEnduranceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix = {}
        for fps in FPS_VALUES:
            for live in MODES:
                for network, wifi in (("lan", False), ("wifi", True)):
                    profile = replace(
                        Profile.from_switches(False, wifi),
                        video_fps=fps,
                    )
                    cls.matrix[fps, live, network] = simulate_scenario(
                        profile,
                        seconds=ENDURANCE_SECONDS,
                        bitrate_bps=4000000,
                        input_bps=8000000,
                        width=1280,
                        height=720,
                        live=live,
                    )

    def test_endurance_matrix_is_bounded_and_explicitly_virtual(self):
        self.assertEqual(len(self.matrix), len(FPS_VALUES) * len(MODES) * 2)
        for key, report in self.matrix.items():
            with self.subTest(key=key):
                self.assertEqual(report["simulation_mode"], "offline_virtual_time")
                self.assertIs(report["hardware_emulator"], False)
                self.assertIs(report["physical_allocation_measured"], False)
                self.assertEqual(report["live"], key[1])
                stats = report["simulation"]
                self.assertEqual(
                    stats["simulated_elapsed_ms"], ENDURANCE_SECONDS * 1000)
                self.assertLessEqual(
                    stats["simulated_presented_frames"],
                    stats["simulated_decoder_frames"])
                self.assertGreater(stats["simulated_presented_frames"], 0)
                self.assertGreaterEqual(stats["simulated_ring_bytes"], 0)
                self.assertLessEqual(
                    stats["simulated_ring_bytes"],
                    stats["simulated_ring_capacity_bytes"])
                self.assertEqual(
                    stats["simulated_ring_capacity_bytes"],
                    expected_ring_bytes(14, key[1]))
                self.assertEqual(
                    stats["simulated_ring_capacity_bytes"] % TS_PACKET_SIZE, 0)
                self.assertGreaterEqual(stats["simulated_frame_queue"], 0)
                self.assertLessEqual(
                    stats["simulated_frame_queue"],
                    stats["simulated_frame_slots"])
                self.assertGreaterEqual(stats["simulated_stalled_ms"], 0)
                self.assertLessEqual(
                    stats["simulated_stalled_ms"],
                    stats["simulated_elapsed_ms"])
                for sample in report["samples"] + [stats]:
                    self.assertTrue(
                        all(name.startswith("simulated_") for name in sample),
                        sorted(sample))

    def test_lan_cadence_tracks_each_declared_frame_rate(self):
        for fps in FPS_VALUES:
            for live in MODES:
                with self.subTest(fps=fps, live=live):
                    stats = self.matrix[fps, live, "lan"]["simulation"]
                    expected = fps * ENDURANCE_SECONDS
                    self.assertLessEqual(
                        abs(stats["simulated_presented_frames"] - expected), 2)
                    self.assertEqual(stats["simulated_stalled_ms"], 0)
                    self.assertGreaterEqual(
                        stats["simulated_ring_overflow_bytes"], 0)

    def test_wifi_endurance_is_bounded_by_lan_and_memory_caps(self):
        for fps in FPS_VALUES:
            for live in MODES:
                with self.subTest(fps=fps, live=live):
                    lan = self.matrix[fps, live, "lan"]["simulation"]
                    wifi = self.matrix[fps, live, "wifi"]["simulation"]
                    self.assertLessEqual(
                        wifi["simulated_presented_frames"],
                        lan["simulated_presented_frames"])
                    self.assertLessEqual(
                        wifi["simulated_ring_bytes"],
                        wifi["simulated_ring_capacity_bytes"])
                    self.assertLessEqual(
                        wifi["simulated_stalled_ms"],
                        wifi["simulated_elapsed_ms"])
                    self.assertGreaterEqual(
                        wifi["simulated_presented_frames"], 0)

    def test_memory_ceiling_and_floor_stay_fixed_for_full_endurance(self):
        cases = (
            (False, 0, packet_aligned(RING_MIN)),
            (False, 14, expected_ring_bytes(14, False)),
            (False, 4096, packet_aligned(RING_MAX)),
            (True, 0, packet_aligned(RING_MIN)),
            (True, 14, packet_aligned(LIVE_RING_MAX)),
            (True, 4096, packet_aligned(LIVE_RING_MAX)),
        )
        for live, free_xdr_mib, expected in cases:
            with self.subTest(live=live, free_xdr_mib=free_xdr_mib):
                self.assertEqual(
                    expected_ring_bytes(free_xdr_mib, live), expected)
                self.assertLessEqual(
                    expected, LIVE_RING_MAX if live else RING_MAX)
                self.assertGreaterEqual(expected, packet_aligned(RING_MIN))
                self.assertEqual(expected % TS_PACKET_SIZE, 0)
                budget = ps3_memory_budget(
                    1920, 1080, free_xdr_mib, live=live)
                self.assertIs(budget["physical_allocation_measured"], False)
                self.assertEqual(
                    budget["assumed_ring_capacity_bytes"], expected)
                profile = replace(
                    Profile.from_switches(False, False),
                    free_xdr_mib=free_xdr_mib,
                )
                report = simulate_scenario(
                    profile, seconds=ENDURANCE_SECONDS, bitrate_bps=10000000,
                    input_bps=20000000, width=1920, height=1080, live=live)
                for sample in report["samples"] + [report["simulation"]]:
                    self.assertEqual(
                        sample["simulated_ring_capacity_bytes"], expected)
                    self.assertLessEqual(
                        sample["simulated_ring_bytes"], expected)

    def test_jittered_arrival_endurance_is_deterministic_and_bounded(self):
        for fps in FPS_VALUES:
            for live in MODES:
                for jitter_ms in JITTER_VALUES:
                    with self.subTest(
                            fps=fps, live=live, jitter_ms=jitter_ms):
                        first = self._run_jittered(fps, live, jitter_ms)
                        second = self._run_jittered(fps, live, jitter_ms)
                        self.assertEqual(first, second)
                        stats, shaper_stats = first
                        self.assertGreaterEqual(
                            stats["simulated_ring_bytes"], 0)
                        self.assertLessEqual(
                            stats["simulated_ring_bytes"],
                            stats["simulated_ring_capacity_bytes"])
                        self.assertEqual(
                            stats["simulated_ring_capacity_bytes"],
                            expected_ring_bytes(14, live))
                        self.assertLessEqual(
                            stats["simulated_frame_queue"],
                            stats["simulated_frame_slots"])
                        self.assertLessEqual(
                            stats["simulated_stalled_ms"],
                            stats["simulated_elapsed_ms"])
                        self.assertGreater(
                            shaper_stats["simulated_wifi_bytes"], 0)
                        self.assertGreaterEqual(
                            shaper_stats[
                                "simulated_wifi_retransmission_pauses"], 0)
                        self.assertLessEqual(
                            shaper_stats["simulated_wifi_delay_ms_total"],
                            JITTER_HORIZON_SECONDS * 1000 + 30000)
                        elapsed_seconds = (
                            stats["simulated_elapsed_ms"] / 1000.0)
                        goodput_kbps = (
                            shaper_stats["simulated_wifi_bytes"] * 8.0 /
                            elapsed_seconds / 1000.0)
                        self.assertAlmostEqual(
                            goodput_kbps, 2500.0, delta=125.0)

    @staticmethod
    def _run_jittered(fps, live, jitter_ms):
        profile = replace(
            Profile.from_switches(False, True),
            video_fps=fps,
            jitter_ms=jitter_ms,
        )
        shaper = NetworkShaper(profile)
        pipeline = Ps3Pipeline(profile, 640, 360, live=live)
        now = 0.0
        while now < JITTER_HORIZON_SECONDS:
            delay = shaper.delay_for(JITTER_CHUNK_BYTES, now=now)
            now += delay
            stats = pipeline.step(
                min(60000.0, delay * 1000.0), JITTER_CHUNK_BYTES)
        return stats, shaper.stats()


if __name__ == "__main__":
    unittest.main()
