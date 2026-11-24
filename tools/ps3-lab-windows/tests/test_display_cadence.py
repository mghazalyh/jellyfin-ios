import unittest
from dataclasses import replace
from ps3lab.model import Profile, Ps3Pipeline, simulate_scenario


class DisplayCadenceTests(unittest.TestCase):
    def test_fractional_frame_rates_on_supported_refresh_modes(self):
        for fps, hz in ((23.976,59.94),(24,59.94),(25,59.94),
                        (29.97,59.94),(30,59.94),(24,60),(25,50),(24,24)):
            with self.subTest(fps=fps,hz=hz):
                p=replace(Profile.from_switches(False,False),video_fps=fps,vblank_hz=hz)
                result=simulate_scenario(p,seconds=30,bitrate_bps=1500000,
                                         input_bps=3000000,width=854,height=480)["simulation"]
                self.assertLessEqual(abs(result["simulated_presented_frames"]-30*fps),2)
                self.assertEqual(result["simulated_stalled_ms"],0)

    def test_three_two_cadence_retains_fractional_duration(self):
        p=replace(Profile.from_switches(False,False),video_fps=23.976,vblank_hz=59.94)
        engine=Ps3Pipeline(p,1280,720,live=False)
        flips=[]
        previous=0
        for i in range(1,121):
            data=engine.step(1000/p.vblank_hz,100000)
            current=data["simulated_presented_frames"]
            if current>previous:
                self.assertEqual(current-previous,1)
                flips.append(i)
            previous=current
        gaps=[b-a for a,b in zip(flips,flips[1:])]
        self.assertTrue(set(gaps).issubset({2,3}),gaps)
        self.assertEqual(set(gaps),{2,3})
        self.assertAlmostEqual(len(flips),120*23.976/59.94,delta=1)

    def test_empty_queue_during_normal_picture_hold_is_not_stall(self):
        p=replace(Profile.from_switches(False,False),video_fps=24,vblank_hz=60,
                  stream_video_bps=192000)
        engine=Ps3Pipeline(p,640,360,live=False)
        # Exactly one nominal decoded picture, no further arrivals.
        engine.step(1000/60,1000)
        engine.step(1000/60)
        first=engine.step(1000/60)
        self.assertEqual(first["simulated_presented_frames"],1)
        held=engine.step(1000/60)
        self.assertEqual(held["simulated_stalled_ms"],0)
        self.assertEqual(held["simulated_presented_frames"],1)
        missed=engine.step(1000/60)
        self.assertGreater(missed["simulated_stalled_ms"],0)
        self.assertEqual(missed["simulated_presented_frames"],1)

    def test_starvation_cannot_accumulate_unbounded_catchup_debt(self):
        p=replace(Profile.from_switches(False,False),video_fps=24,vblank_hz=60)
        engine=Ps3Pipeline(p,1280,720,live=False)
        stalled=engine.step(2000,0)
        self.assertEqual(stalled["simulated_presented_frames"],0)
        self.assertGreater(stalled["simulated_stalled_ms"],1900)
        self.assertLessEqual(engine._frame_phase_ms,1000/p.video_fps)
        count=0
        for _ in range(60):
            result=engine.step(1000/p.vblank_hz,100000)
            self.assertLessEqual(result["simulated_presented_frames"]-count,1)
            count=result["simulated_presented_frames"]
        self.assertLessEqual(count,25)
        self.assertGreaterEqual(count,23)


if __name__=="__main__":
    unittest.main()
