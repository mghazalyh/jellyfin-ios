"""PS3 request parity from v3.6.5-surf.1 console's own C source.

Golden expectations derive from vquality.cpp, stream_budget.h, the
stream_request.cpp C query builder and api_detail.cpp profile bodies.
These tests intentionally distinguish simulated behavior from actual PS3 CPU,
RSX, CellAudio or network instruction execution.
"""
import itertools
import unittest
from urllib.parse import parse_qs, urlsplit

from ps3lab.jellyfin import build_stream_url, ps3_device_profile
from ps3lab.parity import (
    QUALITY_ORDER, audio_cost, audio_params, stream_query, video_budget,
    video_mode,
)


def request(**kwargs):
    return dict(stream_query(device_id="ps3-lab-parity", source_id="src", **kwargs))


class ConsoleQualityParity(unittest.TestCase):
    def test_offered_quality_ladder_matches_physical_ps3(self):
        self.assertEqual(
            QUALITY_ORDER,
            ("auto", "360p", "480p", "720p", "1080p",
             "1080p20", "1080p25"),
        )
        expected = {
            "360p": (640, 360, "baseline", "31", 700000),
            "480p": (854, 480, "baseline", "31", 1500000),
            "720p": (1280, 720, "baseline", "31", 4000000),
            "1080p": (1920, 1080, "high", "42", 10000000),
            "1080p20": (1920, 1080, "high", "42", 20000000),
            "1080p25": (1920, 1080, "high", "42", 25000000),
        }
        for quality, values in expected.items():
            with self.subTest(quality=quality):
                actual = video_mode(quality)
                self.assertEqual(
                    (actual.width, actual.height, actual.profile,
                     actual.level, actual.step_bps),
                    values,
                )
        self.assertEqual(video_mode("auto", hd=False).step_bps, 4000000)
        self.assertEqual(video_mode("auto", hd=True).step_bps, 10000000)
        self.assertEqual(video_mode("original-retired").step_bps, 0)
        with self.assertRaises(ValueError):
            video_mode("random")
        with self.assertRaises(ValueError):
            video_mode("../../escape")

    def test_display_clamping_preserves_high_quality(self):
        self.assertEqual((video_mode("720p", display_width=640,
                                     display_height=480).width,
                          video_mode("720p", display_width=640,
                                     display_height=480).height), (640, 480))
        self.assertEqual((video_mode("1080p20", display_width=640,
                                     display_height=480).width,
                          video_mode("1080p20", display_width=640,
                                     display_height=480).height), (1920, 1080))

    def test_live_uses_high_profile_and_never_uncapped_bitrate(self):
        for quality in QUALITY_ORDER:
            with self.subTest(quality=quality):
                mode = video_mode(quality, live=True)
                self.assertEqual(mode.profile, "high")
                self.assertEqual(mode.level, "42" if mode.height >= 1080 else "41")
                self.assertGreater(mode.step_bps, 0)
                options = request(quality=quality, live=True)
                self.assertEqual(options["AllowVideoStreamCopy"], "false")
                self.assertGreater(options["VideoBitrate"], 0)
        self.assertEqual(video_mode("original-retired", live=True).step_bps,
                         10000000)

    def test_audio_and_transport_budget_matches_c_integer_arithmetic(self):
        for step, audio, expected in (
            (4000000, 192000, 3688000),
            (4000000, 640000, 3240000),
            (10000000, 192000, 9508000),
            (10000000, 640000, 9060000),
            (25000000, 3000000, 21250000),
            (25000000, 4500000, 19750000),
            (1500000, 4500000, 750000),
            (0, 640000, 0),
        ):
            with self.subTest(step=step, audio=audio):
                self.assertEqual(video_budget(step, audio), expected)
        self.assertEqual(audio_cost("dts", 1509000), 3000000)
        self.assertEqual(audio_cost("dts", 5400000), 5400000)
        self.assertEqual(audio_cost("truehd", 2500000), 4500000)
        self.assertEqual(audio_cost("ac3", 640000), 640000)
        with self.assertRaises(ValueError):
            audio_cost("opus", 100000)

    def test_mpeg_ts_requests_preserve_full_quality_ladder(self):
        expected = {
            "360p": (640, 360, "baseline", "31", 487000),
            "480p": (854, 480, "baseline", "31", 1263000),
            "720p": (1280, 720, "baseline", "31", 3688000),
            "1080p": (1920, 1080, "high", "42", 9508000),
            "1080p20": (1920, 1080, "high", "42", 19208000),
            "1080p25": (1920, 1080, "high", "42", 24058000),
        }
        for mode, expected_params in expected.items():
            with self.subTest(mode=mode):
                parsed = request(quality=mode)
                w, h, profile, level, bitrate = expected_params
                self.assertEqual(parsed["MaxWidth"], w)
                self.assertEqual(parsed["MaxHeight"], h)
                self.assertEqual(parsed["Profile"], profile)
                self.assertEqual(parsed["Level"], level)
                self.assertEqual(parsed["VideoBitrate"], bitrate)
                self.assertEqual(parsed["VideoCodec"], "h264")
                self.assertEqual(parsed["AllowAudioStreamCopy"], "false")
                self.assertEqual(parsed["AudioCodec"], "mp3")
                self.assertEqual(parsed["MaxFramerate"], 30)

    def test_audio_variants_exact_fields_and_flags(self):
        cases = [
            ({}, "mp3", "false", 2, 192000),
            ({"surround": True}, "ac3", "false", 6, 640000),
            ({"surround": True, "hd_codec": "dts"},
             "ac3,dts,mp3", "true", 8, None),
            ({"surround": True, "hd_codec": "truehd"},
             "ac3,truehd,mp3", "true", 8, None),
            ({"surround": True, "ac3_copy": True},
             "ac3", "true", 6, None),
        ]
        for settings, codec, copy, channels, bitrate in cases:
            with self.subTest(settings=settings):
                params = request(**settings)
                self.assertEqual(params["AudioCodec"], codec)
                self.assertEqual(params["AllowAudioStreamCopy"], copy)
                self.assertEqual(params["MaxAudioChannels"], channels)
                self.assertEqual(params["AudioSampleRate"], 48000)
                if bitrate is None:
                    self.assertNotIn("AudioBitrate", params)
                else:
                    self.assertEqual(params["AudioBitrate"], bitrate)
        with self.assertRaises(ValueError):
            audio_params(True, hd_codec="flac")

    def test_stream_bitrate_ceiling_copy_and_direct_stream_toggle(self):
        self.assertEqual(request()["VideoBitrate"], 3688000)
        self.assertEqual(request(no_video_copy=True)["AllowVideoStreamCopy"], "false")
        self.assertEqual(request(budget_audio=False)["VideoBitrate"], 4000000)
        original = request(quality="original-retired")
        self.assertNotIn("VideoBitrate", original)
        self.assertEqual(original["MaxWidth"], 1920)
        self.assertEqual(original["AllowVideoStreamCopy"], "true")

    def test_audio_subtitle_indices_and_console_drawn_subtitles(self):
        parsed = request(audio_index=3, subtitle_index=7,
                         subtitle_on_console=False)
        self.assertEqual(parsed["AudioStreamIndex"], 3)
        self.assertEqual(parsed["SubtitleStreamIndex"], 7)
        self.assertEqual(parsed["SubtitleMethod"], "Encode")
        self.assertNotIn("SubtitleStreamIndex", request(
            audio_index=-1, subtitle_index=7, subtitle_on_console=True))
        self.assertNotIn("AudioStreamIndex", request())

    def test_session_id_path_and_encoded_query_specials(self):
        url = build_stream_url(
            "http://localhost:8096", "item/A", "source?version=7",
            "ps3-lab-device", "session/+&=?", "live_1",
            live=False, start_ticks=111222333, audio_index=3,
            subtitle_index=4,
        )
        uri = urlsplit(url)
        parsed = parse_qs(uri.query)
        self.assertEqual(uri.path, "/Videos/source%3Fversion%3D7/stream.ts")
        self.assertEqual(parsed["MediaSourceId"], ["source?version=7"])
        self.assertEqual(parsed["PlaySessionId"], ["session/+&=?"])
        self.assertEqual(parsed["LiveStreamId"], ["live_1"])
        self.assertEqual(parsed["StartTimeTicks"], ["111222333"])
        self.assertEqual(parsed["AudioStreamIndex"], ["3"])
        self.assertEqual(parsed["SubtitleStreamIndex"], ["4"])

    def test_invalid_identifiers_and_negative_seek_are_rejected(self):
        with self.assertRaises(ValueError):
            build_stream_url("http://localhost", "item", "", "device")
        with self.assertRaises(ValueError):
            build_stream_url("http://localhost", "item", "source",
                             "device", start_ticks=-1)
        with self.assertRaises(ValueError):
            request(start_ticks=-1)

    def test_all_four_body_profiles_and_hd_copy_advertising(self):
        for hd, surround, hd_audio in itertools.product((False, True), repeat=3):
            if hd_audio and not surround:
                continue
            with self.subTest(hd=hd, surround=surround, hd_audio=hd_audio):
                profile = ps3_device_profile(hd, surround, hd_audio)
                self.assertEqual(profile["Name"], "PS3")
                self.assertEqual(profile["DirectPlayProfiles"], [])
                self.assertEqual(profile["MaxStreamingBitrate"],
                                 10000000 if hd else 8000000)
                transcode = profile["TranscodingProfiles"][0]
                self.assertEqual(transcode["Container"], "ts")
                self.assertEqual(transcode["VideoCodec"], "h264")
                self.assertEqual(transcode["AudioCodec"],
                    "ac3,mp3,dts,truehd" if hd_audio else
                    "ac3,mp3" if surround else "mp3")
                self.assertEqual(transcode["MaxAudioChannels"],
                    "8" if hd_audio else "6" if surround else "2")
                codecs = [x["Codec"] for x in profile["CodecProfiles"]]
                self.assertIn("h264", codecs)
                self.assertIn("mp3", codecs)
                self.assertEqual("ac3" in codecs, bool(surround))
                self.assertEqual("dts,truehd" in codecs, bool(hd_audio))
                self.assertEqual(profile["SubtitleProfiles"][-1],
                                 {"Format": "dvdsub", "Method": "Encode"})


if __name__ == "__main__":
    unittest.main()
