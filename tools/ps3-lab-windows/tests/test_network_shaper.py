"""Deterministic tests for the client-side 802.11b NetworkShaper model.

These are synthetic host tests. They exercise only virtual time supplied by
the caller; no socket, server, or hardware is involved.
"""
import unittest
from dataclasses import replace

from ps3lab.model import (
    Profile, NetworkShaper, TS_PACKET_SIZE, WIFI_B_PHY_KBPS,
)

REFERENCE_CHUNK = TS_PACKET_SIZE * 64


def wifi_profile(seed=31, probability=0.016, pause_ms=250, severe=False,
                 **changes):
    profile = Profile.from_switches(False, True, severe)
    return replace(
        profile, seed=seed, retry_pause_probability=probability,
        retry_pause_ms=pause_ms, periodic_stall_seconds=0, **changes)


def goodput_kbps(shaper, chunk, seconds=120.0):
    now = 0.0
    sent = 0
    while now < seconds:
        now += shaper.delay_for(chunk, now=now)
        sent += chunk
    return sent * 8.0 / now / 1000.0


class NetworkShaperTests(unittest.TestCase):
    def test_same_seed_and_inputs_are_bit_identical(self):
        def timeline(seed, chunk):
            shaper = NetworkShaper(wifi_profile(seed=seed))
            now = 0.0
            delays = []
            for _ in range(200):
                delay = shaper.delay_for(chunk, now=now)
                delays.append(delay)
                now += delay
            return delays, shaper.stats()

        for chunk in (TS_PACKET_SIZE, REFERENCE_CHUNK, REFERENCE_CHUNK * 10):
            with self.subTest(chunk=chunk):
                self.assertEqual(timeline(7, chunk), timeline(7, chunk))
        self.assertNotEqual(
            timeline(7, REFERENCE_CHUNK)[0],
            timeline(8, REFERENCE_CHUNK)[0])

    def test_zero_length_read_has_no_side_effects(self):
        profile = wifi_profile(probability=1.0)
        shaper = NetworkShaper(profile)
        self.assertEqual(shaper.delay_for(0, now=10.0), 0.0)
        self.assertEqual(shaper.stats()["simulated_wifi_bytes"], 0)
        self.assertEqual(shaper.stats()["simulated_wifi_retransmission_pauses"], 0)
        self.assertEqual(shaper.stats()["simulated_wifi_delay_ms_total"], 0)
        self.assertFalse(shaper._initialized)
        self.assertEqual(shaper._retry_debt, 0.0)

        control = NetworkShaper(profile)
        self.assertEqual(
            shaper.delay_for(REFERENCE_CHUNK, now=10.0),
            control.delay_for(REFERENCE_CHUNK, now=10.0))
        self.assertEqual(
            shaper.stats()["simulated_wifi_retransmission_pauses"],
            control.stats()["simulated_wifi_retransmission_pauses"])
        with self.assertRaises(ValueError):
            shaper.delay_for(-1, now=10.0)

    def test_long_idle_does_not_pay_stale_retry_debt(self):
        profile = wifi_profile(probability=1.0, pause_ms=900)
        shaper = NetworkShaper(profile)
        first = shaper.delay_for(REFERENCE_CHUNK, now=0.0)
        self.assertGreater(shaper._retry_debt, 0.0)
        shaper.profile = replace(profile, retry_pause_probability=0.0)

        nominal = REFERENCE_CHUNK * 8.0 / (profile.effective_wifi_kbps * 1000.0)
        after_idle = shaper.delay_for(REFERENCE_CHUNK, now=first + 60.0)
        self.assertAlmostEqual(after_idle, nominal, places=9)

        start = first + 60.0
        now = start
        sent = 0
        for _ in range(50):
            now += shaper.delay_for(REFERENCE_CHUNK, now=now)
            sent += REFERENCE_CHUNK
        self.assertAlmostEqual(sent * 8.0 / (now - start) / 1000.0,
                               2500.0, delta=2500.0 * 0.05)

    def test_catch_up_burst_is_capped_at_802_11b_phy_rate(self):
        profile = wifi_profile(probability=1.0, pause_ms=900)
        shaper = NetworkShaper(profile)
        first = shaper.delay_for(REFERENCE_CHUNK, now=0.0)
        shaper.profile = replace(profile, retry_pause_probability=0.0)

        nominal = REFERENCE_CHUNK * 8.0 / (profile.effective_wifi_kbps * 1000.0)
        phy = REFERENCE_CHUNK * 8.0 / (WIFI_B_PHY_KBPS * 1000.0)
        now = first
        delays = []
        for _ in range(40):
            delay = shaper.delay_for(REFERENCE_CHUNK, now=now)
            delays.append(delay)
            now += delay
        self.assertLess(min(delays), nominal)
        for delay in delays:
            self.assertGreaterEqual(delay, phy - 1e-9)
            self.assertLessEqual(
                REFERENCE_CHUNK * 8.0 / delay / 1000.0,
                WIFI_B_PHY_KBPS + 1e-6)

    def test_aggregate_goodput_is_independent_of_chunk_size(self):
        chunks = (TS_PACKET_SIZE, 1500, REFERENCE_CHUNK, REFERENCE_CHUNK * 10)
        for seed in (1, 31, 99):
            for chunk in chunks:
                with self.subTest(seed=seed, chunk=chunk):
                    shaper = NetworkShaper(wifi_profile(seed=seed))
                    self.assertAlmostEqual(
                        goodput_kbps(shaper, chunk), 2500.0,
                        delta=2500.0 * 0.05)

    def test_severe_aggregate_goodput_is_independent_of_chunk_size(self):
        for chunk in (TS_PACKET_SIZE, REFERENCE_CHUNK):
            with self.subTest(chunk=chunk):
                shaper = NetworkShaper(wifi_profile(seed=31, severe=True))
                self.assertAlmostEqual(
                    goodput_kbps(shaper, chunk), 1400.0,
                    delta=1400.0 * 0.05)

    def test_retry_debt_stays_within_documented_cap(self):
        profile = wifi_profile(probability=1.0, pause_ms=900)
        shaper = NetworkShaper(profile)
        now = 0.0
        maximum = 0.0
        for _ in range(200):
            now += shaper.delay_for(REFERENCE_CHUNK, now=now)
            maximum = max(maximum, shaper._retry_debt)
        self.assertLessEqual(maximum, 30.0 + 1e-9)
        self.assertEqual(
            shaper.stats()["simulated_wifi_retransmission_pauses"], 200)


if __name__ == "__main__":
    unittest.main()
