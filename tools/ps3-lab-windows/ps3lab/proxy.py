"""Loopback-only streaming player proxy with real client-side network shaping.

Media tokens remain inside the Python process. mpv receives an opaque localhost
URL, NEVER a Jellyfin token or password. Transport data is passed unmodified
unless the operator explicitly enables the separate corruption test.
"""
from __future__ import annotations
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import os
import secrets
import socket
import threading
import time
from .model import NetworkShaper, Profile, Ps3Pipeline, TsInspector
from .parity import TS_OVERHEAD_PERCENT


def requested_stream_workload(url, profile, hd=False):
    """Derive model ceilings from the request, never from assumed decoded media.

    Audio passthrough has no explicit rate, so its transport estimate remains
    incomplete. A fixture without PS3 query fields retains its supplied profile.
    """
    try:
        query = parse_qs(urlsplit(url).query, max_num_fields=128)
    except ValueError:
        query = {}

    def positive_int(key, fallback, maximum):
        try:
            values = query.get(key, [])
            value = int(values[0]) if len(values) == 1 else 0
            return value if 0 < value <= maximum else fallback
        except (TypeError, ValueError):
            return fallback

    width = positive_int('MaxWidth', 1920 if hd else 1280, 4096)
    height = positive_int('MaxHeight', 1080 if hd else 720, 2160)
    video = positive_int('VideoBitrate', profile.stream_video_bps, 100000000)
    audio = positive_int('AudioBitrate', 0, 10000000)
    payload_percent = 100 - TS_OVERHEAD_PERCENT
    transport = (
        ((video + audio) * 100 + payload_percent - 1) // payload_percent
        if audio else profile.stream_transport_bps
    )
    return width, height, replace(profile, stream_video_bps=video,
                                  stream_transport_bps=transport)

class StreamController:
    def __init__(self, emit):
        self._lock = threading.RLock()
        self._emit = emit
        self._generation = 0
        self.profile = Profile.from_switches(False, False)
        self.hd = False
        self.requested_width = 1920
        self.requested_height = 1080
        self._configured = False
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

    def configure(self, url: str, headers: dict, profile: Profile, hd=False, *, live=True):
        with self._lock:
            self._generation += 1
            self.stream_url = url
            self.headers = dict(headers)
            width, height, workload = requested_stream_workload(url, profile, hd)
            self.requested_width, self.requested_height = width, height
            self._configured = True
            self.profile = workload
            self.hd = hd
            self.inspector = TsInspector()
            self.pipeline = Ps3Pipeline(workload, width, height, live=live)
            self._shaper = NetworkShaper(workload)
            self._last_sample = 0.0
            self._last_tick = None
            self._total_bytes = 0
            self._frame_pacing_tail = 0.0
            self.closed.clear()
            self.suspended.clear()

    def set_profile(self, profile: Profile):
        with self._lock:
            if self._configured:
                profile = replace(profile, stream_video_bps=self.profile.stream_video_bps,
                                  stream_transport_bps=self.profile.stream_transport_bps)
            if profile == self.profile:
                return
            self.profile = profile
            self._shaper = NetworkShaper(profile)
            self.pipeline.profile = profile
            self._frame_pacing_tail = 0.0
            self._emit("config_changed", {"mode": profile.name})

    @property
    def generation(self):
        with self._lock:
            return self._generation

    def owns(self, generation):
        with self._lock:
            return (generation == self._generation and
                    not self.closed.is_set())

    def stop(self, generation=None):
        """Cancel the configured stream only if it still owns this generation.

        A stale LocalProxy must not stop a newer session that reused the same
        controller after a reconfigure.
        """
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            self._generation += 1
            self._configured = False
            self.stream_url = ""
            self.headers = {}
            self.closed.set()
            return True

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
                nominal = max(1, self.profile.nominal_transport_bps / 8 /
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
                snap.update(simulated_requested_width=self.requested_width,
                            simulated_requested_height=self.requested_height,
                            simulated_reserved_width=self.pipeline.budget["reserved_width"],
                            simulated_reserved_height=self.pipeline.budget["reserved_height"],
                            simulated_requested_video_bps=self.profile.stream_video_bps,
                            simulated_nominal_transport_bps=self.profile.nominal_transport_bps,
                            simulated_transport_rate_includes_audio=bool(self.profile.stream_transport_bps),
                            simulated_live=self.pipeline.live)
                # Keep measurement channels explicitly separate: PTS/PCR are
                # transport observations, mpv time-pos is a player clock polled
                # over IPC, and this simulator never measures physical output.
                snap.update(transport_measurement_source="mpeg_ts_pts_pcr",
                            mpv_clock_seconds=None,
                            physical_output_frames=None,
                            physical_output_measured=False)
                snap.update(self._shaper.stats())
                self._emit("sample", snap)
            return delay

class LocalProxy:
    def __init__(self, controller: StreamController):
        self.controller = controller
        self.generation = controller.generation
        self.secret_path = "/stream/" + secrets.token_urlsafe(20)
        self._lifecycle = threading.Lock()
        self._active_lock = threading.Lock()
        self._active = set()
        self._connections = 0
        self._connections_done = threading.Event()
        self._connections_done.set()
        self._started = False
        self._closed = False
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
                    generation = owner.controller._generation
                    upstream_url = owner.controller.stream_url
                    headers = dict(owner.controller.headers)
                if not upstream_url or not owner._owns(generation):
                    self.send_error(503)
                    return
                origin = None
                owner._connection_started()
                try:
                    req = Request(upstream_url, headers=headers)
                    origin = urlopen(req, timeout=45)
                    if not owner._owns(generation):
                        return
                    if not owner._register(origin, generation):
                        return
                    try:
                        if not owner._owns(generation):
                            return
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
                        while owner._owns(generation):
                            data = origin.read(188 * 64)
                            if not data or not owner._owns(generation):
                                break
                            if owner.controller.suspended.is_set():
                                while (owner.controller.suspended.is_set() and
                                       not owner.controller.closed.wait(.05)):
                                    if not owner._owns(generation):
                                        break
                            if not owner._owns(generation):
                                break
                            wait = owner.controller.ingest(data)
                            if not owner._owns(generation):
                                break
                            if wait and owner.controller.closed.wait(wait):
                                break
                            self.wfile.write(data)
                        self.wfile.flush()
                    finally:
                        owner._unregister(origin)
                except (HTTPError, URLError, OSError, BrokenPipeError,
                        ConnectionError, ValueError) as ex:
                    # Cancellation of a superseded generation is not an error.
                    if owner._owns(generation):
                        owner.controller._emit("stream_error", {
                            "error": type(ex).__name__, "http": getattr(ex, "code", 0)
                        })
                finally:
                    if origin is not None:
                        try:
                            origin.close()
                        except (OSError, ValueError):
                            pass
                    owner._connection_finished()

            def do_HEAD(self):
                if self.path != owner.secret_path:
                    self.send_error(404)
                    return
                with owner.controller._lock:
                    generation = owner.controller._generation
                if not owner._owns(generation):
                    self.send_error(503)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _owns(self, generation):
        return self.controller.owns(generation)

    def _register(self, origin, generation):
        with self._active_lock:
            if not self._owns(generation):
                return False
            self._active.add(origin)
            return True

    def _unregister(self, origin):
        with self._active_lock:
            self._active.discard(origin)

    def _connection_started(self):
        with self._active_lock:
            self._connections += 1
            self._connections_done.clear()

    def _connection_finished(self):
        with self._active_lock:
            self._connections = max(0, self._connections - 1)
            if not self._connections:
                self._connections_done.set()

    @property
    def active_connections(self):
        with self._active_lock:
            return self._connections

    def _cancel_active(self):
        with self._active_lock:
            active = list(self._active)
            self._active.clear()
        for origin in active:
            sock = getattr(getattr(getattr(origin, "fp", None), "raw", None),
                           "_sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}{self.secret_path}"

    def start(self):
        with self._lifecycle:
            if self._started or self._closed:
                return
            self._started = True
        self._thread.start()

    def close(self):
        with self._lifecycle:
            if self._closed:
                return
            self._closed = True
        self.controller.stop(self.generation)
        self._cancel_active()
        if self._started:
            self.server.shutdown()
        self.server.server_close()
        if self._started:
            self._thread.join(timeout=3)
        self._connections_done.wait(timeout=3)
