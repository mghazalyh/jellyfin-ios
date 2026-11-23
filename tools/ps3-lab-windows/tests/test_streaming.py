"""Fixture-only playback, browse and embedded player regression tests."""
import json
import unittest
from urllib.parse import parse_qs, urlsplit
from ps3lab.jellyfin import JellyfinClient, JellyfinError
from ps3lab.player import build_mpv_args, encode_command, validated_volume, validated_seek


class FixtureClient(JellyfinClient):
    def __init__(self):
        super().__init__("http://127.0.0.1:8096", "ps3-lab-test")
        self.user_id = "fixture-user"
        self.calls = []

    def request(self, path, method="GET", payload=None, timeout=None):
        self.calls.append((path, method, payload))
        if "PlaybackInfo" in path:
            return {"PlaySessionId": "play-session", "MediaSources": [
                {"Id": "source-a"}, {"Id": "source-b"}]}
        return {"Items": [{"Id": "ep-1", "Type": "Episode"}],
                "TotalRecordCount": 1}


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.client = FixtureClient()

    def test_real_library_categories(self):
        for category, expected in (
            ("home", "Movie,Episode"),
            ("movies", "Movie"), ("shows", "Series"),
            ("search", "Movie,Series,Episode")):
            data = self.client.browse(category, limit=30, search="Mars")
            self.assertEqual(data["TotalRecordCount"], 1)
            query = parse_qs(urlsplit(self.client.calls[-1][0]).query)
            self.assertEqual(query["IncludeItemTypes"], [expected])
            self.assertEqual(query["SearchTerm"], ["Mars"])
        self.client.browse("resume")
        self.assertIn("Items/Resume?", self.client.calls[-1][0])
        self.client.browse("live")
        self.assertIn("LiveTv/Channels?", self.client.calls[-1][0])
        with self.assertRaises(JellyfinError):
            self.client.browse("invalid")

    def test_show_episode_endpoint(self):
        episode_list = self.client.series_episodes("series/1")
        self.assertEqual(episode_list["Items"][0]["Id"], "ep-1")
        self.assertIn("Shows/series%2F1/Episodes?", self.client.calls[-1][0])

    def test_resume_start_time_kept_in_playbackinfo_and_transport(self):
        item = {"Id": "movie1", "Type": "Movie",
                "RunTimeTicks": 24000000000}
        session = self.client.make_session(
            item, hd=True, surround=True,
            start_ticks=5100000000, source_id="source-b")
        self.assertEqual(session.source_id, "source-b")
        self.assertEqual(session.start_ticks, 5100000000)
        self.assertEqual(session.duration_ticks, 24000000000)
        self.assertIn("StartTimeTicks=5100000000", self.client.calls[-1][0])
        query = parse_qs(urlsplit(session.url).query)
        self.assertEqual(query["MediaSourceId"], ["source-b"])
        self.assertEqual(query["StartTimeTicks"], ["5100000000"])
        self.assertEqual(query["AudioCodec"], ["ac3"])
        self.assertEqual(query["MaxWidth"], ["1920"])

    def test_live_never_resumes_and_never_video_stream_copies(self):
        item = {"Id": "channel-id", "Type": "TvChannel"}
        session = self.client.make_session(item, start_ticks=4500000000)
        query = parse_qs(urlsplit(session.url).query)
        self.assertEqual(session.start_ticks, 0)
        self.assertEqual(query["StartTimeTicks"], ["0"])
        self.assertEqual(query["AllowVideoStreamCopy"], ["false"])

    def test_mpv_embeds_in_window_not_a_fake_player(self):
        args = build_mpv_args(
            "mpv.exe", "http://127.0.0.1:4000/stream/random",
            123456, r"\\.\pipe\private", platform="win32")
        self.assertIn("--wid=123456", args)
        self.assertIn("--force-window=immediate", args)
        self.assertIn("--input-ipc-server=" + r"\\.\pipe\private", args)
        self.assertEqual(args[-1], "http://127.0.0.1:4000/stream/random")
        with self.assertRaises(ValueError):
            build_mpv_args("mpv", "https://remote.example/movie", 123, "pipe")

    def test_mpv_ipc_command_protocol(self):
        data = json.loads(encode_command(["get_property", "time-pos"], 17))
        self.assertEqual(data["request_id"], 17)
        self.assertEqual(data["command"][0], "get_property")
        self.assertEqual(validated_seek(900), 300)
        self.assertEqual(validated_seek(-600), -300)
        self.assertEqual(validated_volume(-5), 0)
        self.assertEqual(validated_volume(None), 80)


if __name__ == "__main__":
    unittest.main()
