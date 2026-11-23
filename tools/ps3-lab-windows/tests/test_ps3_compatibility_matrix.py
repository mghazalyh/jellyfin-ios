"""PS3 protocol-derived virtual CPU/RSX and 802.11b compatibility matrix.

Virtual-time stress predicts pipeline behavior. No console instructions are run.
"""
from __future__ import annotations
import unittest
from ps3lab.model import Profile, simulate_scenario

FORMATS = (("360p",640,360,700000),("720p",1280,720,4000000),("1080p",1920,1080,10000000))
STRESS = (("lan",False,False,False),
          ("cell-rsx-stress",True,False,False),
          ("wifi-80211b",False,True,False),
          ("severe-both",True,True,True))


class VirtualConsoleCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix = {}
        for quality, width, height, bitrate in FORMATS:
            for name, cpu, wifi, severe in STRESS:
                report = simulate_scenario(
                    Profile.from_switches(cpu, wifi, severe=severe),
                    seconds=30, bitrate_bps=bitrate, input_bps=bitrate*2,
                    width=width, height=height)
                cls.matrix[(quality,name)] = report

    def test_all_twelve_scenarios_remain_bounded_and_nonhardware(self):
        self.assertEqual(len(self.matrix),12)
        for key, report in self.matrix.items():
            with self.subTest(key=key):
                self.assertIs(report["hardware_emulator"],False)
                stats=report["simulation"]
                self.assertEqual(stats["simulated_elapsed_ms"],30000)
                self.assertGreater(stats["simulated_presented_frames"],0)
                self.assertGreaterEqual(stats["simulated_decoder_frames"],stats["simulated_presented_frames"])
                self.assertGreater(stats["simulated_ring_capacity_bytes"],0)
                self.assertGreaterEqual(stats["simulated_ring_bytes"],0)
                self.assertLessEqual(stats["simulated_ring_bytes"],stats["simulated_ring_capacity_bytes"])
                self.assertGreaterEqual(stats["simulated_stalled_ms"],0)
                self.assertLessEqual(stats["simulated_stalled_ms"],30000)
                self.assertGreaterEqual(stats["simulated_flip_late_count"],0)

    def test_low_bandwidth_causes_higher_resolution_starvation(self):
        stats={key: report["simulation"] for key,report in self.matrix.items()}
        self.assertEqual(stats[("360p","wifi-80211b")]["simulated_stalled_ms"],0)
        self.assertGreater(stats[("720p","wifi-80211b")]["simulated_stalled_ms"],0)
        self.assertGreater(stats[("1080p","wifi-80211b")]["simulated_stalled_ms"],
                           stats[("720p","wifi-80211b")]["simulated_stalled_ms"])
        for quality,*_ in FORMATS:
            with self.subTest(quality=quality):
                self.assertLessEqual(
                    stats[(quality,"wifi-80211b")]["simulated_presented_frames"],
                    stats[(quality,"lan")]["simulated_presented_frames"])
                self.assertLessEqual(
                    stats[(quality,"severe-both")]["simulated_presented_frames"],
                    stats[(quality,"wifi-80211b")]["simulated_presented_frames"])

    def test_rsx_stress_is_visible_in_diagnostics(self):
        for quality,*_ in FORMATS:
            with self.subTest(quality=quality):
                lan=self.matrix[(quality,"lan")]["simulation"]
                stressed=self.matrix[(quality,"cell-rsx-stress")]["simulation"]
                severe=self.matrix[(quality,"severe-both")]["simulation"]
                self.assertEqual(lan["simulated_flip_late_count"],0)
                self.assertGreater(stressed["simulated_flip_late_count"],0)
                self.assertGreater(severe["simulated_flip_late_count"],0)
                self.assertFalse(lan["simulated_cpu_gpu_enabled"])
                self.assertTrue(stressed["simulated_cpu_gpu_enabled"])


if __name__ == "__main__":
    unittest.main()
