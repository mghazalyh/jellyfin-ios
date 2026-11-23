"""Native macOS mpv args and Unix-domain IPC test; fixture only."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from ps3lab.player import build_mpv_args, ipc_command, new_ipc_path


class MacPlaybackTests(unittest.TestCase):
    def test_cocoa_player_window_has_no_foreign_hwnd(self):
        args = build_mpv_args("mpv",
            "http://127.0.0.1:17342/stream/safe", 0,
            "/tmp/mpv-ipc-test.sock", platform="darwin")
        self.assertEqual(args[0], "mpv")
        self.assertEqual(args[-1], "http://127.0.0.1:17342/stream/safe")
        self.assertIn("--osc=yes", args)
        self.assertIn("--vo=gpu-next", args)
        self.assertFalse(any(x.startswith("--wid=") for x in args))
        with self.assertRaises(ValueError):
            build_mpv_args("mpv", "https://remote.invalid/movie",
                           0, "sock", platform="darwin")

    def test_windows_native_hwnd_still_works(self):
        args = build_mpv_args("mpv.exe",
            "http://127.0.0.1:1777/stream/safe",
            559221, r"\\.\pipe\test", platform="win32")
        self.assertIn("--wid=559221", args)
        self.assertIn("--osc=no", args)
        self.assertNotIn("--vo=gpu-next", args)

    @unittest.skipIf(os.name == "nt", "Unix sockets are not used on Windows")
    def test_real_unix_socket_json_ipc_and_event_filtering(self):
        with tempfile.TemporaryDirectory(prefix="jf-ps3-ipc-") as folder:
            path = str(Path(folder) / "player.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            listener.listen(1)
            requests = []
            def serve():
                try:
                    client, _ = listener.accept()
                    with client:
                        with client.makefile("rb") as reader:
                            payload = json.loads(reader.readline())
                        requests.append(payload)
                        client.sendall(b'{"event":"start-file"}\n')
                        reply = {"request_id": payload["request_id"],
                                 "error": "success", "data": 12.75}
                        client.sendall((json.dumps(reply) + "\n").encode())
                finally:
                    listener.close()
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            result = ipc_command(path, ["get_property", "time-pos"])
            worker.join(5)
            self.assertEqual(result["data"], 12.75)
            self.assertEqual(requests[0]["command"], ["get_property", "time-pos"])
            self.assertGreater(requests[0]["request_id"], 0)

    @unittest.skipIf(os.name == "nt", "Unix sockets are not used on Windows")
    def test_short_and_unique_socket_path(self):
        first, second = new_ipc_path(), new_ipc_path()
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("/tmp/"))
        self.assertLess(len(first), 104)


if __name__ == "__main__":
    unittest.main()
