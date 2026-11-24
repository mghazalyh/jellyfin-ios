"""Deterministic, observable PS3 playback timing model.

References in mghazalyh/JellyFin-PS3 tag v3.6.5-surf.1:
video.h (188-byte TS packets, 8/24 YUV420 slots, prefill min(12, cap)),
core/live_buffer.h (4/8/15 s reserve, 10 MiB live ring),
threads/player_threads.cpp (2-40 MiB read-ahead, live 10 MiB),
core/player_display.cpp (duration-consumption, RSX sync and flips),
surf_gate_logic.h (scene-cut thresholds; 3 s safety recovery in surf_sync.cpp).
RPCS3: RSXThread.cpp, RSXFIFO.cpp, cellAudio.cpp and cellVideoOut.cpp.

This is intentionally NOT cycle-accurate RSX/Cell emulation. Measurements
emitted by this simulator are always named simulated_*, and physical metrics
are never invented. Pure Python for reproducible GitHub-hosted test runs.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from collections import deque
import random
import threading
import time

TS_PACKET_SIZE = 188
LIVE_RING_MAX = 10 * 1024 * 1024
RING_MIN = 2 * 1024 * 1024
RING_MAX = 40 * 1024 * 1024
VIDEO_PES_MAX = 1536 * 1024 + 64 * 1024
AUDIO_PES_MAX = 64 * 1024
SOURCE_GATE_MAX_MS = 3000
LIVE_RESERVE_SECONDS = (4, 8, 15)

@dataclass(frozen=True)
class Profile:
    name: str
    simulate_cpu_gpu: bool = False
    simulate_wifi_b: bool = False
    # These are adjustable stress parameters, not measured physical constants.
    cpu_decode_ms: float = 11.0
    rsx_upload_ms: float = 3.0
    rsx_flip_ms: float = 1.0
    rsx_queue_depth: int = 2
    vblank_hz: float = 59.94
    video_fps: float = 24.0
    effective_wifi_kbps: int = 2500
    base_rtt_ms: float = 45.0
    jitter_ms: float = 30.0
    retry_pause_probability: float = 0.016
    retry_pause_ms: int = 250
    periodic_stall_seconds: int = 0
    stream_video_bps: int = 4000000
    free_xdr_mib: int = 14
    seed: int = 31
    # Optional requested multiplexed rate, not a measured actual rate.
    stream_transport_bps: int = 0

    @property
    def nominal_transport_bps(self):
        return self.stream_transport_bps or self.stream_video_bps

    @classmethod
    def from_switches(cls, cpu_gpu: bool, wifi_b: bool, severe: bool = False):
        return cls(
            name=("PS3 CPU+GPU+RSX" if cpu_gpu else "Normal CPU+GPU") +
                 (" + 2.4GHz 802.11b" if wifi_b else " + LAN"),
            simulate_cpu_gpu=cpu_gpu,
            simulate_wifi_b=wifi_b,
            cpu_decode_ms=35.0 if severe and cpu_gpu else (23.0 if cpu_gpu else 11.0),
            rsx_upload_ms=28.0 if severe and cpu_gpu else (13.0 if cpu_gpu else 3.0),
            rsx_flip_ms=14.0 if severe and cpu_gpu else (8.0 if cpu_gpu else 1.0),
            effective_wifi_kbps=1400 if severe and wifi_b else 2500,
            retry_pause_probability=0.06 if severe and wifi_b else 0.016,
            retry_pause_ms=900 if severe and wifi_b else 250,
            periodic_stall_seconds=12 if severe and wifi_b else 0,
        )

def frame_buffer_bytes(width: int, height: int) -> int:
    if width < 1 or height < 1 or width > 4096 or height > 2160:
        raise ValueError("invalid frame dimensions")
    return ((width + 15) // 16 * 16) * ((height + 15) // 16 * 16) * 3 // 2

def jitter_slots(width: int, height: int) -> int:
    return 8 if frame_buffer_bytes(width, height) > 2 * 1024 * 1024 else 24

def live_buffer_target(bits_per_second: int, capacity_packets: int, seconds: int) -> int:
    if capacity_packets <= 0:
        return 0
    rate = bits_per_second or 4000000
    packets = (rate * seconds + 1503) // 1504
    maximum = capacity_packets - capacity_packets // 10
    return min(maximum, max(1, packets))

def ps3_memory_budget(width: int, height: int, free_xdr_mib: int = 14) -> dict:
    """Model reserved allocations; never represent actual free physical RAM."""
    slot = frame_buffer_bytes(width, height)
    count = jitter_slots(width, height)
    reserve = count * slot
    # The compressed ring gets three quarters of free memory after frame reservation.
    proposed = max(RING_MIN, free_xdr_mib * 1024 * 1024 * 3 // 4)
    ring = min(LIVE_RING_MAX, RING_MAX, proposed)
    return {
        "video_frame_bytes": slot, "decoded_slots": count,
        "decoded_reserved_bytes": reserve, "live_ring_bytes": ring,
        "live_ring_packets": ring // TS_PACKET_SIZE,
        "prefill_frames": min(12, count),
        "video_pes_buffer_bytes": VIDEO_PES_MAX,
        "audio_pes_buffer_bytes": AUDIO_PES_MAX,
        "assumed_remaining_xdr_mib": free_xdr_mib,
        "source": "v3.6.5-surf.1",
    }

class NetworkShaper:
    """Client-side, TCP-like 802.11b stress: bounded throughput + retries.

    Do NOT randomly remove MPEG-TS packets: real Wi-Fi MAC/TCP retransmits
    lost packets. To simulate TS corruption, use the explicit corruption test.
    """
    def __init__(self, profile: Profile):
        self.profile = profile
        self._rng = random.Random(profile.seed)
        self._lock = threading.Lock()
        self._next_delivery = 0.0
        self._initialized = False
        self._bytes = 0
        self._retries = 0
        self._delay_ms = 0.0
        self._last_period = -1

    def delay_for(self, count: int, now: float | None = None) -> float:
        """Return seconds to sleep; deterministic for fixed input timestamps."""
        if count < 0:
            raise ValueError("negative byte count")
        if not self.profile.simulate_wifi_b:
            return 0.0
        with self._lock:
            clock = time.monotonic() if now is None else now
            kbps = max(256, self.profile.effective_wifi_kbps)
            transmission = count * 8.0 / (kbps * 1000.0)
            # One initial link RTT only. Applying a fresh 45ms on each
            # 12KB transport read would turn 2.5 Mbps into ~1.1 Mbps
            # and seriously misrepresent the intended stress profile.
            if not self._initialized:
                initial = (self.profile.base_rtt_ms + self._rng.uniform(
                    -self.profile.jitter_ms, self.profile.jitter_ms)) / 1000.0
                self._next_delivery = clock + max(0.0, initial)
                self._initialized = True
            self._next_delivery = max(clock, self._next_delivery) + transmission
            if self._rng.random() < self.profile.retry_pause_probability:
                self._next_delivery += self.profile.retry_pause_ms / 1000.0
                self._retries += 1
            if self.profile.periodic_stall_seconds:
                period = int(clock // self.profile.periodic_stall_seconds)
                if period != self._last_period:
                    if self._last_period >= 0:
                        self._next_delivery += self.profile.retry_pause_ms / 1000.0
                    self._last_period = period
            delay = max(0.0, self._next_delivery - clock)
            self._bytes += count
            self._delay_ms += delay * 1000.0
            return delay

    def stats(self):
        with self._lock:
            return {
                "simulated_wifi_bytes": self._bytes,
                "simulated_wifi_retransmission_pauses": self._retries,
                "simulated_wifi_delay_ms_total": round(self._delay_ms),
                "wifi_standard": "2.4 GHz IEEE 802.11b effective-rate model"
                if self.profile.simulate_wifi_b else "no shaping",
            }

class TsInspector:
    """188-byte MPEG-TS framing, PAT/PMT, PCR, PES PTS and continuity tracker.

    This observer never changes transport bytes. Supports chunk boundary
    reassembly and maintains bounded state, including a 187-byte remainder.
    """
    def __init__(self):
        self.pending = bytearray()
        self.packets = 0
        self.bytes = 0
        self.sync_losses = 0
        self.continuity_errors = 0
        self.transport_errors = 0
        self.scrambled_packets = 0
        self.null_packets = 0
        self.pat_packets = 0
        self.pmt_packets = 0
        self.video_packets = 0
        self.audio_packets = 0
        self.discontinuities = 0
        self.pcr_first = None
        self.pcr_last = None
        self.pcr_duration_ms = 0
        self.pcr_pid = None
        self.pmt_pid = None
        self.video_pid = None
        self.audio_pid = None
        self.video_pts = None
        self.audio_pts = None
        self.pes_video_starts = 0
        self.pes_audio_starts = 0
        self._cc: dict[int, int] = {}

    @staticmethod
    def _pts(payload: bytes) -> int | None:
        if len(payload) < 14 or payload[:3] != b"\x00\x00\x01":
            return None
        if not (payload[7] & 0x80):
            return None
        return (
            ((payload[9] & 0x0e) >> 1) << 30 |
            payload[10] << 22 |
            ((payload[11] & 0xfe) >> 1) << 15 |
            payload[12] << 7 |
            ((payload[13] & 0xfe) >> 1)
        )

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self.bytes += len(data)
        self.pending.extend(data)
        while len(self.pending) >= TS_PACKET_SIZE:
            if self.pending[0] != 0x47:
                offset = self.pending.find(b"\x47", 1)
                if offset < 0:
                    discard = max(1, len(self.pending) - TS_PACKET_SIZE + 1)
                else:
                    discard = offset
                del self.pending[:discard]
                self.sync_losses += discard
                continue
            pkt = bytes(self.pending[:TS_PACKET_SIZE])
            del self.pending[:TS_PACKET_SIZE]
            self._packet(pkt)

    def _packet(self, p: bytes) -> None:
        self.packets += 1
        if p[1] & 0x80:
            self.transport_errors += 1
        pid = (p[1] & 0x1f) << 8 | p[2]
        pusi = bool(p[1] & 0x40)
        if pid == 0x1fff:
            self.null_packets += 1
            return
        if p[3] & 0xc0:
            self.scrambled_packets += 1
        adaptation = (p[3] >> 4) & 3
        if adaptation == 0:
            self.transport_errors += 1
            return
        payload = bool(adaptation & 1)
        off = 4
        discontinuity = False
        if adaptation & 2:
            length = p[4]
            if 5 + length > TS_PACKET_SIZE:
                self.transport_errors += 1
                return
            if length:
                discontinuity = bool(p[5] & 0x80)
                if discontinuity:
                    self.discontinuities += 1
                if length >= 7 and p[5] & 0x10:
                    pcr = (p[6] << 25 | p[7] << 17 | p[8] << 9 |
                           p[9] << 1 | p[10] >> 7)
                    if self.pcr_pid is None:
                        self.pcr_pid, self.pcr_first = pid, pcr
                    if self.pcr_pid == pid:
                        if self.pcr_last is not None and self.pcr_first is not None:
                            if discontinuity or pcr < self.pcr_last or pcr - self.pcr_last > 900000:
                                self.pcr_duration_ms += max(0, (self.pcr_last - self.pcr_first) // 90)
                                self.pcr_first = pcr
                        self.pcr_last = pcr
            off = 5 + length
        cc = p[3] & 15
        if payload:
            prev = self._cc.get(pid)
            if prev is not None and not discontinuity and cc != ((prev + 1) & 15):
                self.continuity_errors += 1
            self._cc[pid] = cc
        if not payload or off >= TS_PACKET_SIZE:
            return
        data = p[off:]
        if pid == 0:
            self.pat_packets += 1
            if pusi and data:
                pos = 1 + data[0]
                section = data[pos:]
                if len(section) >= 12 and section[0] == 0:
                    end = min(len(section), 3 + (((section[1] & 15) << 8) | section[2]) - 4)
                    for i in range(8, end - 3, 4):
                        if section[i:i+2] != b"\x00\x00":
                            self.pmt_pid = (section[i+2] & 31) << 8 | section[i+3]
                            break
        if pid == self.pmt_pid and self.pmt_pid is not None:
            self.pmt_packets += 1
            if pusi and data:
                pos = 1 + data[0]
                sec = data[pos:]
                if len(sec) >= 12 and sec[0] == 2:
                    end = min(len(sec), 3 + (((sec[1] & 15) << 8) | sec[2]) - 4)
                    es = 12 + (((sec[10] & 15) << 8) | sec[11])
                    while es + 4 < end:
                        stream_type = sec[es]
                        spid = (sec[es+1] & 31) << 8 | sec[es+2]
                        ln = (sec[es+3] & 15) << 8 | sec[es+4]
                        if stream_type == 0x1b and self.video_pid is None:
                            self.video_pid = spid
                        elif stream_type in (3,4,15,0x81,0x06,0x82,0x87) and self.audio_pid is None:
                            self.audio_pid = spid
                        es += 5 + ln
        if pid == self.video_pid and self.video_pid is not None:
            self.video_packets += 1
            if pusi:
                self.pes_video_starts += 1
                self.video_pts = self._pts(data)
        if pid == self.audio_pid and self.audio_pid is not None:
            self.audio_packets += 1
            if pusi:
                self.pes_audio_starts += 1
                self.audio_pts = self._pts(data)

    def snapshot(self) -> dict:
        pcr_ms = self.pcr_duration_ms
        if self.pcr_first is not None and self.pcr_last is not None:
            pcr_ms += max(0, (self.pcr_last - self.pcr_first) // 90)
        av_ms = None
        if self.video_pts is not None and self.audio_pts is not None:
            diff = ((self.video_pts - self.audio_pts + (1 << 32)) % (1 << 33))
            if diff >= (1 << 32):
                diff -= 1 << 33
            av_ms = round(diff / 90.0, 2)
        return {
            "ts_packets": self.packets, "transport_bytes": self.bytes,
            "sync_skipped_bytes": self.sync_losses,
            "continuity_errors": self.continuity_errors,
            "transport_errors": self.transport_errors,
            "scrambled_packets": self.scrambled_packets,
            "null_packets": self.null_packets,
            "pmt_pid": self.pmt_pid, "video_pid": self.video_pid,
            "audio_pid": self.audio_pid,
            "pat_packets": self.pat_packets, "pmt_packets": self.pmt_packets,
            "video_packets": self.video_packets, "audio_packets": self.audio_packets,
            "video_pes_starts": self.pes_video_starts,
            "audio_pes_starts": self.pes_audio_starts,
            "pcr_elapsed_ms": pcr_ms, "pts_video_minus_audio_ms": av_ms,
            "discontinuity_flags": self.discontinuities,
            "unparsed_tail_bytes": len(self.pending),
        }

class Ps3Pipeline:
    """Budget-accurate queues plus adjustable, deterministic timing stress.

    Byte injection stands for network arrival, not decoding on Cell.
    Model-derived fields always use simulated_* prefixes.
    """
    def __init__(self, profile: Profile, width=1920, height=1080, live=True):
        self.profile = profile
        self.budget = ps3_memory_budget(width, height, profile.free_xdr_mib)
        self.live = live
        self._ring = 0.0
        self._frames = 0.0
        self._rsx_pending = deque()
        self._at_ms = 0.0
        self._decode_credit_ms = 0.0
        self._present_credit_ms = 0.0
        self._input_bytes = 0
        self._overflow_bytes = 0
        self._stalled_ms = 0.0
        self._decoded = 0
        self._displayed = 0
        self._dropped = 0
        self._vblanks = 0
        self._sync_waits = 0
        self._flip_late = 0
        self._last_present_ms = 0.0
        self._max_frame_queue = 0
        self._last_snapshot = {}

    def step(self, elapsed_ms: float, received_bytes: int = 0) -> dict:
        if not 0 <= elapsed_ms <= 60000 or received_bytes < 0:
            raise ValueError("invalid simulator step")
        self._at_ms += elapsed_ms
        self._input_bytes += received_bytes
        ring_max = self.budget["live_ring_bytes"] if self.live else RING_MAX
        offered = self._ring + received_bytes
        self._overflow_bytes += max(0.0, offered - ring_max)
        self._ring = min(ring_max, offered)
        # CPU stage: compressed bytes per decoded frame and limited frame slots.
        frame_interval = 1000.0 / max(1.0, self.profile.video_fps)
        effective_decode = max(0.5, self.profile.cpu_decode_ms)
        self._decode_credit_ms = min(500.0, self._decode_credit_ms + elapsed_ms)
        self._present_credit_ms += elapsed_ms
        nominal_bytes_per_frame = max(1, self.profile.nominal_transport_bps / 8.0 /
                                      max(1.0, self.profile.video_fps))
        cap = self.budget["decoded_slots"]
        while (self._decode_credit_ms >= effective_decode and
               self._ring >= nominal_bytes_per_frame and self._frames < cap):
            self._decode_credit_ms -= effective_decode
            self._ring -= nominal_bytes_per_frame
            self._frames += 1
            self._decoded += 1
            self._max_frame_queue = max(self._max_frame_queue, int(self._frames))
        period = 1000.0 / max(1.0, self.profile.vblank_hz)
        # RSX upload/flip queue represents asynchronous GPU command scheduling.
        while self._present_credit_ms >= period:
            self._present_credit_ms -= period
            vblank_time = self._at_ms - self._present_credit_ms
            self._vblanks += 1
            while self._rsx_pending and self._rsx_pending[0] <= vblank_time:
                self._rsx_pending.popleft()
            if self._rsx_pending:
                self._sync_waits += 1
            if self._frames < 1:
                self._stalled_ms += period
                continue
            if self._rsx_pending and len(self._rsx_pending) >= max(1, self.profile.rsx_queue_depth):
                self._flip_late += 1
                continue
            if vblank_time - self._last_present_ms < frame_interval:
                continue
            rsx_ms = self.profile.rsx_upload_ms + self.profile.rsx_flip_ms
            # Frames held due to GPU upload are counted as missed flips.
            if rsx_ms > period:
                self._flip_late += 1
            self._rsx_pending.append(vblank_time + rsx_ms)
            self._frames -= 1
            self._displayed += 1
            self._last_present_ms = vblank_time
        result = {
            "simulated_elapsed_ms": round(self._at_ms),
            "simulated_received_bytes": self._input_bytes,
            "simulated_ring_bytes": round(self._ring),
            "simulated_ring_capacity_bytes": ring_max,
            "simulated_ring_overflow_bytes": round(self._overflow_bytes),
            "simulated_decoder_frames": self._decoded,
            "simulated_presented_frames": self._displayed,
            "simulated_frame_queue": round(self._frames, 2),
            "simulated_frame_slots": cap,
            "simulated_max_queued_frames": self._max_frame_queue,
            "simulated_vblanks": self._vblanks,
            "simulated_stalled_ms": round(self._stalled_ms),
            "simulated_flip_late_count": self._flip_late,
            "simulated_rsx_sync_waits": self._sync_waits,
            "simulated_dropped_frames": self._dropped,
            "simulated_cpu_gpu_enabled": self.profile.simulate_cpu_gpu,
        }
        self._last_snapshot = result
        return result

    def snapshot(self):
        return dict(self._last_snapshot)

def simulate_scenario(profile: Profile, seconds=20, bitrate_bps=4000000,
                      input_bps=3000000, width=1920, height=1080) -> dict:
    """Virtual-time deterministic offline simulation for GitHub CI."""
    from dataclasses import replace
    p = replace(profile, stream_video_bps=bitrate_bps, stream_transport_bps=0)
    engine = Ps3Pipeline(p, width=width, height=height)
    trace = []
    for index in range(int(seconds * 10)):
        # A reproducible transport burst/throughput source; no wallclock sleep.
        burst_factor = 0.35 if 32 <= index % 100 < 52 else 1.0
        if p.simulate_wifi_b:
            available = min(input_bps, p.effective_wifi_kbps * 1000)
        else:
            available = input_bps
        n = int(available / 8 / 10 * burst_factor)
        trace.append(engine.step(100, n))
    return {
        "profile": asdict(p),
        "simulation": trace[-1] if trace else engine.step(0),
        "samples": [trace[i] for i in range(0, len(trace), 10)],
        "hardware_emulator": False,
        "reference": "mghazalyh/JellyFin-PS3 v3.6.5-surf.1",
    }
