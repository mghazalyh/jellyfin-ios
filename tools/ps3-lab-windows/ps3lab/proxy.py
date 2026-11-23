"""Loopback-only streaming player proxy with real client-side network shaping.

Media tokens remain inside the Python process. mpv receives an opaque localhost
URL, NEVER a Jellyfin token or password. Transport data is passed unmodified
unless the operator explicitly enables the separate corruption test.
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import os
import secrets
import threading
import time
from .model import NetworkShaper, Profile, Ps3Pipeline, TsInspector

class StreamController:
    def __init__(self, emit):
        self._lock = threading.RLock()
        self._emit = emit
        self.profile = Profile.from_switches(False, False)
        self.hd = False
        self.stream_url = ""
        self.headers = {}
        self.suspended = threading.Event()
        self.closed = threading.Event()
        self.inspector = TsInspector()
        self.pipeline = Ps3Pipeline(self.profile)
        self._shaper = NetworkShaper(self.profile)
        self._last_sample = 0.0
        self._last_tick = None
        self._total_bytes = 0
        self._frame_pacing_tail = 0.0

    def configure(self, url: str, headers: dict, profile: Profile, hd=False):
        with self._lock:
            self.stream_url = url
            self.headers = dict(headers)
            self.profile = profile
            self.hd = hd
            self.inspector = TsInspector()
            self.pipeline = Ps3Pipeline(profile, 1920 if hd else 1280,
                                        1080 if hd else 720)
            self._shaper = NetworkShaper(profile)
            self._last_sample = 0.0
            self._last_tick = None
            self._total_bytes = 0
            self._frame_pacing_tail = 0.0
            self.closed.clear()
            self.suspended.clear()

    def set_profile(self, profile: Profile):
        with self._lock:
            if profile == self.profile:
                return
            self.profile = profile
            self._shaper = NetworkShaper(profile)
            self.pipeline.profile = profile
            self._frame_pacing_tail = 0.0
            self._emit("config_changed", {"mode": profile.name})

    def stop(self):
        self.closed.set()

    def ingest(self, data: bytes):
        with self._lock:
            now = time.monotonic()
            dt = max(0.0, min(60.0, now - (self._last_tick or now)))
            self._last_tick = now
            self.inspector.feed(data)
            self._total_bytes += len(data)
            model_stats = self.pipeline.step(dt * 1000.0, len(data))
            delay = self._shaper.delay_for(len(data), now)
            # Optional CPU/RSX stress is APPLIED to the playback transport
            # *as a surrogate*. This cannot induce real RSX FIFO behavior on a PC.
            if self.profile.simulate_cpu_gpu:
                nominal = max(1, self.profile.stream_video_bps / 8 /
                              max(1.0, self.profile.video_fps))
                per_frame = max(0.0, self.profile.cpu_decode_ms +
                                self.profile.rsx_upload_ms + self.profile.rsx_flip_ms -
                                1000.0 / self.profile.video_fps)
                delay += min(1.0, len(data) / nominal * per_frame / 1000.0)
            if now - self._last_sample >= 1.0:
                self._last_sample = now
                snap = {}
                snap.update(self.inspector.snapshot())
                snap.update(model_stats)
                snap.update(self._shaper.stats())
                self._emit("sample", snap)
            return delay

class LocalProxy:
    def __init__(self, controller: StreamController):
        self.controller = controller
        self.secret_path = "/stream/" + secrets.token_urlsafe(20)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            def log_message(self, fmt, *args):
                # No request URLs, Authorization headers or secrets in logs.
                return

            def do_GET(self):
                if self.path != owner.secret_path:
                    self.send_error(404)
                    return
                with owner.controller._lock:
                    upstream_url = owner.controller.stream_url
                    headers = dict(owner.controller.headers)
                if not upstream_url or owner.controller.closed.is_set():
                    self.send_error(503)
                    return
                try:
                    req = Request(upstream_url, headers=headers)
                    with urlopen(req, timeout=45) as origin:
                        self.send_response(origin.status)
                        self.send_header("Content-Type", "video/mp2t")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        owner.controller._emit("upstream_http", {
                            "status": origin.status,
                            "content_type": origin.headers.get("Content-Type", "")[:64]
                        })
                        # Align chunks with 188-byte MPEG-TS packet boundaries.
                        while not owner.controller.closed.is_set():
                            data = origin.read(188 * 64)
                            if not data:
                                break
                            if owner.controller.suspended.is_set():
                                while owner.controller.suspended.is_set() and not owner.controller.closed.wait(.05):
                                    pass
                            wait = owner.controller.ingest(data)
                            if wait and owner.controller.closed.wait(wait):
                                break
                            self.wfile.write(data)
                        self.wfile.flush()
                except (HTTPError, URLError, OSError, BrokenPipeError, ConnectionError) as ex:
                    owner.controller._emit("stream_error", {
                        "error": type(ex).__name__, "http": getattr(ex, "code", 0)
                    })

            def do_HEAD(self):
                if self.path != owner.secret_path:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}{self.secret_path}"

    def start(self):
        self._thread.start()

    def close(self):
        self.controller.stop()
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=3)
