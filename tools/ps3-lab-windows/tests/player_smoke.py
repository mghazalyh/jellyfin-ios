"""Actual Windows MPEG-TS decode and embedded mpv video smoke on GitHub CI.

A local fixture server serves an H.264/MP3 transport stream. The PS3-style
loopback proxy inspects/paces it, and real mpv must decode and advance video
inside a Tk native window. Neither a real Jellyfin server nor a PS3 is touched.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ps3lab.model import Profile
from ps3lab.player import build_mpv_args, ipc_command, new_ipc_path
from ps3lab.proxy import LocalProxy, StreamController


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Usage: player_smoke.py <fixture.ts> <mpv.exe>")
    fixture = Path(sys.argv[1])
    mpv = Path(sys.argv[2])
    data = fixture.read_bytes()
    if len(data) < 10000 or len(data) % 188 != 0:
        raise AssertionError("MPEG-TS fixture invalid or too small")
    if not mpv.is_file():
        raise FileNotFoundError("portable mpv missing")

    class Source(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/Videos/fixture/stream.ts":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                for offset in range(0, len(data), 188 * 64):
                    self.wfile.write(data[offset:offset + 188 * 64])
            except (OSError, ConnectionError):
                pass

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = tk.Tk()
    root.title("PS3 Stream Native Player CI")
    root.geometry("960x600+0+0")
    host = tk.Frame(root, bg="black", width=900, height=500)
    host.pack(fill="both", expand=True)
    if sys.platform != "darwin":
        root.update()

    events = []
    controller = StreamController(lambda name, info: events.append((name, info)))
    upstream = f"http://127.0.0.1:{server.server_port}/Videos/fixture/stream.ts"
    controller.configure(upstream, {
        "Authorization": 'MediaBrowser Client="PS3", Device="PS3", DeviceId="ps3-lab-ci"',
        "User-Agent": "JellyfinPS3/0.1",
    }, Profile.from_switches(False, False))
    proxy = LocalProxy(controller)
    proxy.start()
    pipe = new_ipc_path()
    proc = None
    logfile = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    logfile.close()
    loghandle = open(logfile.name, "wb")
    try:
        # macOS mpv owns a Cocoa window; it never uses the Tk native ID.
        # Avoid Tk.update() on macOS 15's legacy system Tk 8.5, which can hang.
        window_id = 0 if sys.platform == "darwin" else int(host.winfo_id())
        args = build_mpv_args(str(mpv), proxy.url, window_id, pipe)
        if sys.platform == "darwin":
            # The macOS process cannot embed its Cocoa window in Tk. Headless
            # testing verifies actual H.264 decode and IPC on a CI runner.
            args.insert(-1, "--vo=null")
            args.insert(-1, "--ao=null")
        args.insert(1, "--log-file=" + logfile.name)
        proc = subprocess.Popen(args, cwd=str(mpv.parent),
                                stdout=subprocess.DEVNULL, stderr=loghandle)
        deadline = time.monotonic() + 18
        max_position = 0.0
        video_params = None
        while time.monotonic() < deadline:
            if sys.platform != "darwin":
                root.update()
            if proc.poll() is not None:
                if max_position >= 1.0 and video_params:
                    break
                raise AssertionError("mpv exited before video playback was confirmed")
            try:
                result = ipc_command(pipe, ["get_property", "time-pos"])
                position = float(result.get("data") or 0)
                max_position = max(position, max_position)
            except (RuntimeError, ValueError, TypeError):
                pass
            try:
                result = ipc_command(pipe, ["get_property", "video-params"])
                if isinstance(result.get("data"), dict):
                    video_params = result.get("data")
            except RuntimeError:
                pass
            if video_params and max_position >= 1.0:
                break
            time.sleep(0.15)
        if not video_params or max_position < 1.0:
            raise AssertionError(
                "mpv failed to decode/present fixture video: " +
                "position=" + str(max_position) +
                " has_video=" + str(bool(video_params)))
        sample = controller.inspector.snapshot()
        if sample.get("ts_packets", 0) < 50:
            raise AssertionError("PS3 transport proxy did not inspect fixture")
        print("NATIVE_MPV_VIDEO_PASS:",
              "decoded_width=", video_params.get("w"),
              "decoded_height=", video_params.get("h"),
              "played_seconds=", round(max_position, 2),
              "ts_packets=", sample["ts_packets"])
    except Exception:
        try:
            lines = Path(logfile.name).read_text(
                encoding="utf-8", errors="replace").splitlines()
            print("MPV_LOG_TAIL")
            for line in lines[-35:]:
                print(line[:320])
        except OSError:
            pass
        raise
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=4)
        loghandle.close()
        proxy.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        root.destroy()
        try:
            Path(logfile.name).unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()
