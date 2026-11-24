import unittest
from ps3lab.model import Profile, simulate_scenario, TS_PACKET_SIZE
from ps3lab.parity import QUALITY_ORDER, video_mode

STRESS = (
    ("lan", False, False, False),
    ("cell-rsx-stress", True, False, False),
    ("wifi-80211b", False, True, False),
    ("severe-both", True, True, True),
)


class FullQualityMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix = {}
        for live in (False, True):
            for quality in QUALITY_ORDER:
                mode = video_mode(quality, live=live)
                for name, cpu, wifi, severe in STRESS:
                    report = simulate_scenario(
                        Profile.from_switches(cpu, wifi, severe),
                        seconds=30, bitrate_bps=mode.step_bps,
                        input_bps=mode.step_bps*2, width=mode.width,
                        height=mode.height, live=live)
                    cls.matrix[live, quality, name] = report

    def test_all_56_cases_are_bounded_and_explicitly_virtual(self):
        self.assertEqual(len(self.matrix), 56)
        for (live, quality, stress), report in self.matrix.items():
            with self.subTest(live=live,quality=quality,stress=stress):
                self.assertFalse(report["hardware_emulator"])
                self.assertFalse(report["physical_allocation_measured"])
                self.assertEqual(report["live"],live)
                mode=video_mode(quality,live=live)
                self.assertEqual(report["requested_dimensions"],(mode.width,mode.height))
                reserved=(1920,1080) if live else (mode.width,mode.height)
                self.assertEqual(report["reserved_dimensions"],reserved)
                for s in report["samples"]+[report["simulation"]]:
                    self.assertLessEqual(s["simulated_ring_bytes"],s["simulated_ring_capacity_bytes"])
                    self.assertEqual(s["simulated_ring_capacity_bytes"]%TS_PACKET_SIZE,0)
                    self.assertGreaterEqual(s["simulated_ring_bytes"],0)
                    self.assertLessEqual(s["simulated_frame_queue"],s["simulated_frame_slots"])
                    self.assertLessEqual(s["simulated_presented_frames"],s["simulated_decoder_frames"])
                    self.assertLessEqual(s["simulated_stalled_ms"],s["simulated_elapsed_ms"])
                    self.assertGreaterEqual(s["simulated_stalled_ms"],0)

    def test_lan_and_wifi_quality_order(self):
        for live in (False,True):
            with self.subTest(live=live):
                for quality in QUALITY_ORDER:
                    lan=self.matrix[live,quality,"lan"]["simulation"]
                    self.assertAlmostEqual(lan["simulated_presented_frames"],720,delta=2)
                    self.assertEqual(lan["simulated_stalled_ms"],0)
                    wifi=self.matrix[live,quality,"wifi-80211b"]["simulation"]
                    severe=self.matrix[live,quality,"severe-both"]["simulation"]
                    self.assertLessEqual(severe["simulated_presented_frames"],wifi["simulated_presented_frames"])
                    self.assertLessEqual(wifi["simulated_presented_frames"],lan["simulated_presented_frames"])
                for quality in ("360p","480p"):
                    self.assertEqual(self.matrix[live,quality,"wifi-80211b"]["simulation"]["simulated_stalled_ms"],0)
                counts=[self.matrix[live,q,"wifi-80211b"]["simulation"]["simulated_presented_frames"]
                        for q in ("720p","1080p","1080p20","1080p25")]
                self.assertEqual(counts,sorted(counts,reverse=True))


if __name__=="__main__":
    unittest.main()
