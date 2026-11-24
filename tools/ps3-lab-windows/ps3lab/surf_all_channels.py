"""Deterministic Surf All Channels end-to-end fixture harness.

This module joins the desktop harness's existing pure-Python contracts into
one replayable path:

    Surf source observation -> source change -> synthetic MPEG-TS
    -> PCR/PTS discontinuity -> audio clock re-anchor -> playback position
    -> decoded luma scene cut -> queued flip -> completed presentation ack
    -> audio release -> controller event

It never opens a socket, never starts mpv, never uses real media and emits no
physical-console measurements.  Synthetic bytes stand for transport arrival;
``physical_*``/``actual_*`` fields stay explicitly unavailable rather than
zero.  A successful host fixture is therefore transport evidence, not proof
that a physical PS3 decoded or presented a picture.

Console references used for the contract:
  source/player/surf_sync.cpp and surf_gate_logic.h (3 s gate recovery,
  luma threshold, presentation-gated audio release);
  docs/ps3-ios-parity.md (a URL change alone is not a source epoch);
  engineering-backlog SURF_ALL_CHANNELS_ACCEPTANCE.md (console controls,
  malformed/absent measurements, and NOT IMPLEMENTED reporting).
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field

from .model import TS_PACKET_SIZE, TsInspector, av_sync_ms
from .sourcegate import SurfAudioGate, sequence_reached

SCHEMA = "JFI-SURF-ALL-CHANNELS-FIXTURE-1"
REFERENCE = "mghazalyh/JellyFin-PS3 v3.6.5-surf.1"
GATE_TIMEOUT_MS = 3000
CONTROL_DEBOUNCE_MS = 250
PTS_CLOCK_HZ = 90
PTS_WRAP = 1 << 33
PTS_HALF_WRAP = 1 << 32

# Controls the desktop harness can exercise without a console or server.
# "REJECTED_LIVE" is a real tested behavior: Surf is a live stream.
DESKTOP_CONTROLS = {
    "play": {"console": "START/Play", "desktop": "IMPLEMENTED", "live": True},
    "pause": {"console": "Pause", "desktop": "IMPLEMENTED", "live": True},
    "resume": {"console": "Resume", "desktop": "IMPLEMENTED", "live": True},
    "stop": {"console": "Stop", "desktop": "IMPLEMENTED", "live": True},
    "toggle_sync": {"console": "Adaptive Audio Sync Beta", "desktop": "IMPLEMENTED", "live": True},
    "seek": {"console": "Seek (not offered on live TV)", "desktop": "REJECTED_LIVE", "live": False},
}

# Console controls that have no desktop implementation.  They must be
# reported NOT IMPLEMENTED, never counted as a passing fixture control.
UNSUPPORTED_CONTROLS = {
    "start_menu": "no console Options/START menu in the desktop harness",
    "dpad_navigation": "no console D-pad focus model in the desktop harness",
    "select_current": "no console X selection of the publishing source",
    "select_previous": "no console recent-source selection control",
    "favorite_current": "no CatalogService/Favorites UI",
    "favorite_selected_previous": "no immutable 32-character selection token control",
    "show_surf_source_status": "no published-source status dialog",
    "toggle_surf_looping": "no Pause/Resume Surf Looping control",
}

# Measurements a host fixture can never establish for the physical console.
UNAVAILABLE_MEASUREMENTS = {
    "actual_mpv_av_offset_ms": None,
    "physical_rsx_flip": None,
    "physical_cellaudio_dma": None,
    "physical_presentation_latency_ms": None,
    "physical_free_xdr_bytes": None,
    "server_source_epoch": None,
}


def signed_pts_delta(current, previous):
    """Signed 33-bit PTS delta, correct across wrap in both directions."""
    if current is None or previous is None:
        return None
    diff = int(current) - int(previous)
    if diff >= PTS_HALF_WRAP:
        diff -= PTS_WRAP
    elif diff <= -PTS_HALF_WRAP:
        diff += PTS_WRAP
    return diff


def pts_to_ms(pts):
    return None if pts is None else float(pts) / PTS_CLOCK_HZ


def _pts_bytes(pts):
    value = int(pts) & (PTS_WRAP - 1)
    return bytes([
        0x21 | (((value >> 30) & 0x07) << 1),
        (value >> 22) & 0xFF,
        0x01 | (((value >> 15) & 0x7F) << 1),
        (value >> 7) & 0xFF,
        0x01 | ((value & 0x7F) << 1),
    ])


def _ts_header(pid, pusi, adaptation, cc):
    return bytes([
        0x47,
        ((pid >> 8) & 0x1F) | (0x40 if pusi else 0),
        pid & 0xFF,
        ((adaptation & 0x03) << 4) | (cc & 0x0F),
    ])


class TsFixtureBuilder:
    """Build exact 188-byte MPEG-TS packets for the existing TsInspector.

    The stream has one H.264 video PID, one AAC audio PID and a PCR on the
    video PID.  Continuity counters are per-PID.  No clock or network is
    involved, so two builders with identical arguments produce identical
    bytes.
    """

    def __init__(self, video_pid=0x101, audio_pid=0x102, pmt_pid=0x100):
        self.video_pid = video_pid
        self.audio_pid = audio_pid
        self.pmt_pid = pmt_pid
        self._cc = {}

    def _next_cc(self, pid):
        cc = self._cc.get(pid, 0)
        self._cc[pid] = (cc + 1) & 0x0F
        return cc

    @staticmethod
    def _pad(packet):
        if len(packet) > TS_PACKET_SIZE:
            raise ValueError("TS packet exceeds 188 bytes")
        return packet + b"\xff" * (TS_PACKET_SIZE - len(packet))

    def _payload_packet(self, pid, payload, pusi=True, corrupt=False,
                        scrambled=False):
        header = _ts_header(pid, pusi, 1, self._next_cc(pid))
        packet = bytearray(self._pad(header + payload[:184]))
        if corrupt:
            packet[1] |= 0x80
        if scrambled:
            packet[3] |= 0x80
        return bytes(packet)

    def pat(self):
        section = bytes([
            0x00, 0xB0, 0x0D, 0x00, 0x01, 0xC1, 0x00, 0x00,
            0x00, 0x01,
            0xE0 | ((self.pmt_pid >> 8) & 0x1F), self.pmt_pid & 0xFF,
            0x00, 0x00, 0x00, 0x00,
        ])
        return self._payload_packet(0, b"\x00" + section, pusi=True)

    def pmt(self, pcr_pid=None):
        pcr_pid = self.video_pid if pcr_pid is None else pcr_pid
        es_video = bytes([0x1B, (self.video_pid >> 8) & 0x1F,
                          self.video_pid & 0xFF, 0xF0, 0x00])
        es_audio = bytes([0x0F, (self.audio_pid >> 8) & 0x1F,
                          self.audio_pid & 0xFF, 0xF0, 0x00])
        section_length = 9 + len(es_video) + len(es_audio) + 4
        section = bytearray()
        section.append(0x02)
        section.append(0xB0 | ((section_length >> 8) & 0x0F))
        section.append(section_length & 0xFF)
        section += b"\x00\x01"                      # program number
        section.append(0xC1)                        # version/current
        section.append(0x00)                        # section number
        section.append(0x00)                        # last section
        section += bytes([(pcr_pid >> 8) & 0x1F, pcr_pid & 0xFF])
        section += b"\xf0\x00"                      # program info length
        section += es_video + es_audio
        section += b"\x00\x00\x00\x00"              # CRC placeholder
        return self._payload_packet(self.pmt_pid, b"\x00" + bytes(section),
                                    pusi=True)

    def pcr(self, base, discontinuity=False):
        base = int(base) & (PTS_WRAP - 1)
        flags = 0x10 | (0x80 if discontinuity else 0)
        adaptation = bytes([
            7, flags,
            (base >> 25) & 0xFF,
            (base >> 17) & 0xFF,
            (base >> 9) & 0xFF,
            (base >> 1) & 0xFF,
            ((base & 1) << 7) | 0x7E,
            0x00,
        ])
        header = _ts_header(self.video_pid, False, 2,
                            self._cc.get(self.video_pid, 0))
        return self._pad(header + adaptation)

    def _pes(self, pid, stream_id, pts, payload_tail=b"\x00" * 16,
             corrupt=False):
        payload = bytearray()
        payload += b"\x00\x00\x01" + bytes([stream_id])
        payload += b"\x00\x00"           # packet length (bounded fixture)
        payload += b"\x80\x80\x05"       # marker, PTS only, header length 5
        payload += _pts_bytes(pts)
        payload += payload_tail
        return self._payload_packet(pid, bytes(payload), pusi=True,
                                    corrupt=corrupt)

    def video_pes(self, pts, corrupt=False):
        return self._pes(self.video_pid, 0xE0, pts, corrupt=corrupt)

    def audio_pes(self, pts, corrupt=False):
        return self._pes(self.audio_pid, 0xC0, pts, corrupt=corrupt)

    def corrupt_packet(self, pid=None):
        pid = self.video_pid if pid is None else pid
        return self._payload_packet(pid, b"\x00" * 32, pusi=False,
                                    corrupt=True)


class AudioClock:
    """Deterministic synthetic audio clock with explicit re-anchor reasons."""

    def __init__(self):
        self.epoch = None
        self.last_video_pts = None
        self.last_audio_pts = None
        self.reset_count = 0
        self.last_reset_reason = None
        self.last_delta_ms = None
        self.last_elapsed_ms = None

    def observe(self, video_pts, audio_pts, epoch=0, discontinuity=False):
        if video_pts is None or audio_pts is None:
            return {"clock": "unavailable", "reset": False,
                    "delta_ms": None, "elapsed_ms": None,
                    "reason": "missing_pts"}
        reason = None
        if self.epoch is None or int(epoch) != int(self.epoch):
            reason = "epoch_change"
        elif discontinuity:
            reason = "discontinuity"
        else:
            delta_ticks = signed_pts_delta(audio_pts, self.last_audio_pts)
            if delta_ticks is not None and delta_ticks < -PTS_CLOCK_HZ * 1000:
                reason = "backward_reset"
        if reason:
            self.epoch = int(epoch)
            self.last_video_pts = int(video_pts)
            self.last_audio_pts = int(audio_pts)
            self.reset_count += 1
            self.last_reset_reason = reason
            self.last_delta_ms = av_sync_ms(video_pts, audio_pts)
            self.last_elapsed_ms = None
            return {
                "clock": "reset", "reset": True, "reason": reason,
                "epoch": self.epoch, "video_pts": int(video_pts),
                "audio_pts": int(audio_pts),
                "delta_ms": self.last_delta_ms, "elapsed_ms": None,
                "resets": self.reset_count,
            }
        delta_ticks = signed_pts_delta(audio_pts, self.last_audio_pts) or 0
        self.last_video_pts = int(video_pts)
        self.last_audio_pts = int(audio_pts)
        self.last_delta_ms = av_sync_ms(video_pts, audio_pts)
        self.last_elapsed_ms = delta_ticks / float(PTS_CLOCK_HZ)
        return {
            "clock": "running", "reset": False, "reason": None,
            "epoch": int(epoch), "video_pts": int(video_pts),
            "audio_pts": int(audio_pts), "delta_ms": self.last_delta_ms,
            "elapsed_ms": self.last_elapsed_ms, "resets": self.reset_count,
        }


class PlaybackPosition:
    """PTS-derived position that re-anchors on epochs and discontinuities."""

    def __init__(self):
        self.epoch = None
        self.last_pts = None
        self.position_ms = None
        self.reset_count = 0
        self.last_reset_reason = None
        self.monotonic = True

    def observe(self, pts, epoch=0, discontinuity=False):
        if pts is None:
            return {"position_ms": self.position_ms, "reset": False,
                    "reason": "missing_pts", "resets": self.reset_count,
                    "monotonic": self.monotonic}
        reason = None
        if self.epoch is None or int(epoch) != int(self.epoch):
            reason = "epoch_change"
        elif discontinuity:
            reason = "discontinuity"
        else:
            delta = signed_pts_delta(pts, self.last_pts)
            if delta is not None and delta < -PTS_CLOCK_HZ * 1000:
                reason = "backward_reset"
        value = max(0.0, pts_to_ms(pts))
        if reason:
            self.reset_count += 1
            self.last_reset_reason = reason
            self.position_ms = value
            self.epoch = int(epoch)
            self.last_pts = int(pts)
            return {"position_ms": self.position_ms, "reset": True,
                    "reason": reason, "resets": self.reset_count,
                    "monotonic": self.monotonic}
        if self.position_ms is not None and value < self.position_ms:
            self.monotonic = False
        self.position_ms = value
        self.last_pts = int(pts)
        return {"position_ms": self.position_ms, "reset": False,
                "reason": None, "resets": self.reset_count,
                "monotonic": self.monotonic}


@dataclass
class Transition:
    """Evidence for one Surf source observation/transition."""

    index: int
    event: str
    at_ms: int
    source_id: str
    boot_id: str
    route: str
    epoch: int
    gate_started: bool = False
    gate_started_ms: int | None = None
    scene_cut: bool = False
    scene_cut_ms: int | None = None
    candidate_sequence: int | None = None
    queued_flip_sequence: int | None = None
    queued_flip_ms: int | None = None
    presentation_ack: dict | None = None
    release_reason: str | None = None
    released: bool = False
    timeout: bool = False
    timeout_ms: int | None = None
    stale_flip_ignored: bool = False
    audio_clock_reset: int = 0
    audio_reset_reason: str | None = None
    signed_offset_ms: float | None = None
    position_ms: float | None = None
    notes: list = field(default_factory=list)

    def as_dict(self):
        return {
            "transition": self.index, "event": self.event,
            "at_ms": self.at_ms, "source_id": self.source_id,
            "boot_id": self.boot_id, "route": self.route,
            "epoch": self.epoch, "gate_started": self.gate_started,
            "gate_started_ms": self.gate_started_ms,
            "scene_cut": self.scene_cut,
            "scene_cut_ms": self.scene_cut_ms,
            "candidate_sequence": self.candidate_sequence,
            "queued_flip_sequence": self.queued_flip_sequence,
            "queued_flip_ms": self.queued_flip_ms,
            "presentation_ack": self.presentation_ack,
            "release_reason": self.release_reason,
            "released": self.released, "timeout": self.timeout,
            "timeout_ms": self.timeout_ms,
            "stale_flip_ignored": self.stale_flip_ignored,
            "audio_clock_reset": self.audio_clock_reset,
            "audio_reset_reason": self.audio_reset_reason,
            "signed_offset_ms": self.signed_offset_ms,
            "position_ms": self.position_ms,
            "notes": list(self.notes),
        }


class SurfAllChannelsHarness:
    """Event-driven synthetic replay of the Surf All Channels contracts."""

    def __init__(self):
        self.generation = 0
        self.epoch = 0
        self.source = None
        self.route = ""
        self.gate = SurfAudioGate()
        self.inspector = TsInspector()
        self.audio_clock = AudioClock()
        self.position = PlaybackPosition()
        self.builder = TsFixtureBuilder()
        self.transitions = []
        self.events = []
        self.observations = []
        self.controls = []
        self.pending_flips = {}
        self._pending_transition = None
        self._pending_clock_reset = False
        self._last_control = {}
        self.paused = False
        self.stopped = False
        self.sync_enabled = False
        self.counters = {
            "source_observations": 0, "source_transitions": 0,
            "route_only_changes": 0, "stale_observations": 0,
            "gate_starts": 0, "scene_cuts": 0, "queued_flips": 0,
            "presentation_acks": 0, "audio_releases": 0, "timeouts": 0,
            "failed_flips": 0, "stale_flips": 0, "premature_releases": 0,
            "audio_clock_resets": 0, "position_resets": 0,
            "duplicate_controls": 0, "rejected_controls": 0,
            "not_implemented_controls": 0,
        }

    def _record(self, kind, at_ms, **data):
        entry = {"event": kind, "at_ms": int(at_ms)}
        entry.update(data)
        self.events.append(entry)
        return entry

    def _transition(self):
        return self._pending_transition

    def start(self, generation=1, sync_enabled=False):
        self.generation = int(generation)
        self.epoch = 0
        self.source = None
        self.route = ""
        self.gate.session(self.generation)
        self.sync_enabled = bool(sync_enabled)
        self.gate.set_enabled(self.sync_enabled)
        self.paused = False
        self.stopped = False
        self._pending_clock_reset = True
        self._record("playback_start", 0, generation=self.generation,
                     sync_enabled=self.sync_enabled)
        return self.sync_enabled

    def observe_source(self, source_id, boot_id="", route="", at_ms=0,
                       generation=None):
        if generation is not None and int(generation) != self.generation:
            self.counters["stale_observations"] += 1
            self._record("source_observation_stale", at_ms,
                         generation=int(generation),
                         current_generation=self.generation)
            return "stale_generation"
        if not source_id:
            self._record("source_observation_empty", at_ms)
            return "ignored"
        previous_source = self.source
        previous_route = self.route
        epoch_before = self.epoch
        event = self.gate.observe(self.generation, str(source_id),
                                  str(boot_id or ""))
        identity = (str(source_id), str(boot_id or ""))
        self.counters["source_observations"] += 1
        if event == "initial_source":
            self.source = identity
            self.route = str(route or "")
            self.epoch = int(self.gate.epoch)
            self._pending_clock_reset = True
            transition = Transition(
                len(self.transitions) + 1, event, int(at_ms),
                identity[0], identity[1], self.route, self.epoch)
            transition.notes.append("initial source observation")
            self.transitions.append(transition)
            self._pending_transition = transition
        elif event in ("transition_observed", "observed_sync_disabled"):
            if event == "transition_observed":
                self.counters["source_transitions"] += 1
            self.source = identity
            self.route = str(route or "")
            self.epoch = int(self.gate.epoch)
            self._pending_clock_reset = True
            transition = Transition(
                len(self.transitions) + 1, event, int(at_ms),
                identity[0], identity[1], self.route, self.epoch)
            if event == "observed_sync_disabled":
                transition.notes.append("audio gate disabled for this source change")
            self.transitions.append(transition)
            self._pending_transition = transition
        elif event == "unchanged":
            if previous_source is not None and identity == previous_source:
                if str(route or "") != previous_route:
                    self.counters["route_only_changes"] += 1
                    self._record("route_changed_identity_stable", at_ms,
                                 epoch=self.epoch, route=route)
                    event = "route_changed_identity_stable"
                else:
                    event = "duplicate_source_observation"
            else:
                event = "duplicate_source_observation"
        elif event == "ignored":
            self.counters["stale_observations"] += 1
        self.observations.append({
            "at_ms": int(at_ms), "source_id": str(source_id),
            "boot_id": str(boot_id or ""), "route": str(route or ""),
            "event": event, "epoch": self.epoch,
            "epoch_before": epoch_before,
            "generation": self.generation,
        })
        self._record("source_observation", at_ms, event=event,
                     epoch=self.epoch, source_id=str(source_id),
                     boot_id=str(boot_id or ""), route=str(route or ""))
        return event

    def tick(self, now_ms, paused=None, passthrough=False):
        paused = self.paused if paused is None else bool(paused)
        event = self.gate.tick(int(now_ms), paused=paused,
                               passthrough=passthrough)
        transition = self._transition()
        if event == "audio_gate_started":
            self.counters["gate_starts"] += 1
            if transition:
                transition.gate_started = True
                transition.gate_started_ms = int(now_ms)
        elif event == "gate_timeout_recovery":
            self.counters["timeouts"] += 1
            if transition:
                transition.timeout = True
                transition.timeout_ms = int(now_ms)
                transition.release_reason = "gate_timeout_recovery"
                transition.notes.append("degraded: 3 s safety timeout, no presentation ack")
        elif event == "bitstream_gate_skipped":
            if transition:
                transition.notes.append("bitstream passthrough skipped PCM gate")
        self._record("gate_tick", now_ms, result=event, paused=paused,
                     gating=self.gate.gating)
        return event

    def feed_ts(self, data, now_ms=0, discontinuity=False):
        before = self.inspector.snapshot()
        self.inspector.feed(data)
        after = self.inspector.snapshot()
        detected = (after["discontinuity_flags"] > before["discontinuity_flags"]
                    or discontinuity)
        if detected:
            self._pending_clock_reset = True
        transition = self._transition()
        if detected and transition:
            transition.notes.append("transport discontinuity observed")
        video_pts = self.inspector.video_pts
        audio_pts = self.inspector.audio_pts
        pts_ready = video_pts is not None and audio_pts is not None
        clock = None
        position = None
        if pts_ready:
            reset = self._pending_clock_reset
            clock = self.audio_clock.observe(
                video_pts, audio_pts, self.epoch, discontinuity=reset)
            position = self.position.observe(
                video_pts, self.epoch, discontinuity=reset)
            if clock["reset"]:
                self.counters["audio_clock_resets"] += 1
                if transition:
                    transition.audio_clock_reset += 1
                    transition.audio_reset_reason = clock["reason"]
            if position["reset"]:
                self.counters["position_resets"] += 1
            self._pending_clock_reset = False
            if transition:
                transition.signed_offset_ms = clock["delta_ms"]
                transition.position_ms = position["position_ms"]
        self._record("ts_feed", now_ms, bytes=len(data),
                     packets=after["ts_packets"],
                     discontinuity=detected,
                     continuity_errors=after["continuity_errors"],
                     transport_errors=after["transport_errors"],
                     sync_skipped_bytes=after["sync_skipped_bytes"],
                     pcr_elapsed_ms=after["pcr_elapsed_ms"],
                     video_pts=video_pts, audio_pts=audio_pts,
                     clock=clock, position=position)
        return after

    def decoded_picture(self, sequence, luma, now_ms=0):
        transition = self._transition()
        was_cut = bool(self.gate.cut_found)
        event = self.gate.decoded_picture(int(sequence), bytes(luma))
        if event == "visual_cut_decoded" and not was_cut:
            self.counters["scene_cuts"] += 1
            if transition:
                transition.scene_cut = True
                transition.scene_cut_ms = int(now_ms)
                transition.candidate_sequence = int(sequence)
        self._record("decoded_picture", now_ms, sequence=int(sequence),
                     event=event, gating=self.gate.gating,
                     cut_found=self.gate.cut_found)
        return event

    def queue_flip(self, sequence, now_ms=0):
        transition = self._transition()
        index = transition.index if transition else None
        self.pending_flips[int(sequence)] = {
            "transition": index, "at_ms": int(now_ms),
        }
        self.counters["queued_flips"] += 1
        if transition:
            transition.queued_flip_sequence = int(sequence)
            transition.queued_flip_ms = int(now_ms)
        # A queued flip is explicitly not a presentation acknowledgement.
        self._record("flip_queued", now_ms, sequence=int(sequence),
                     gating=self.gate.gating,
                     released=not self.gate.gating)
        return "queued"

    def flip_complete(self, success, sequence, now_ms=0):
        transition = self._transition()
        queued = self.pending_flips.pop(int(sequence), None)
        if queued and transition and queued["transition"] != transition.index:
            self.counters["stale_flips"] += 1
            if transition:
                transition.stale_flip_ignored = True
                transition.notes.append(
                    "obsolete transition sequence did not release the current gate")
            self._record("flip_complete_stale", now_ms, sequence=int(sequence),
                         completed_transition=queued["transition"],
                         current_transition=transition.index)
            # Still ask the gate: it must also reject the obsolete candidate.
        event = self.gate.flip_complete(bool(success), int(sequence))
        if event == "video_flip_audio_release":
            self.counters["presentation_acks"] += 1
            self.counters["audio_releases"] += 1
            if transition:
                transition.presentation_ack = {
                    "success": True, "sequence": int(sequence),
                    "at_ms": int(now_ms),
                }
                transition.release_reason = event
                transition.released = True
        elif not success:
            self.counters["failed_flips"] += 1
            if transition:
                transition.notes.append("failed flip did not release audio")
        self._record("flip_complete", now_ms, success=bool(success),
                     sequence=int(sequence), result=event,
                     gating=self.gate.gating,
                     released=event == "video_flip_audio_release")
        if event == "video_flip_audio_release" and transition and queued is None:
            transition.notes.append("release without a queued host flip")
        return event

    def control(self, name, now_ms=0, generation=None, value=None):
        name = str(name)
        previous = self._last_control.get(name)
        if generation is not None and int(generation) != self.generation:
            self.counters["stale_observations"] += 1
            self._record("control_stale", now_ms, control=name,
                         generation=int(generation))
            return "stale_generation"
        if name in UNSUPPORTED_CONTROLS:
            self.counters["not_implemented_controls"] += 1
            entry = {
                "control": name, "at_ms": int(now_ms),
                "status": "NOT IMPLEMENTED",
                "reason": UNSUPPORTED_CONTROLS[name],
                "counted_as_tested": False,
            }
            self.controls.append(entry)
            self._record("control_not_implemented", now_ms, control=name,
                         reason=UNSUPPORTED_CONTROLS[name])
            return "not_implemented"
        if name not in DESKTOP_CONTROLS:
            self.counters["rejected_controls"] += 1
            self._record("control_unknown", now_ms, control=name)
            return "unknown"
        if previous and int(now_ms) - previous < CONTROL_DEBOUNCE_MS:
            self.counters["duplicate_controls"] += 1
            self._record("control_duplicate_ignored", now_ms,
                         control=name, previous_ms=previous)
            return "duplicate_ignored"
        self._last_control[name] = int(now_ms)
        status = "accepted"
        if name == "play":
            self.paused = False
        elif name == "pause":
            self.paused = True
        elif name == "resume":
            self.paused = False
        elif name == "stop":
            self.stopped = True
            status = "stopped"
        elif name == "seek":
            # Surf is a live stream; the desktop harness must not claim seek.
            status = "rejected_live"
            self.counters["rejected_controls"] += 1
        elif name == "toggle_sync":
            self.gate.set_enabled(not self.gate.enabled)
            self.sync_enabled = self.gate.enabled
            status = "enabled" if self.sync_enabled else "disabled"
        self.controls.append({
            "control": name, "at_ms": int(now_ms), "status": status,
            "value": value, "generation": self.generation,
            "counted_as_tested": True,
        })
        self._record("control", now_ms, control=name, status=status,
                     value=value)
        return status

    def snapshot(self):
        inspector = self.inspector.snapshot()
        return {
            "generation": self.generation, "epoch": self.epoch,
            "source": list(self.source) if self.source else None,
            "sync_enabled": self.sync_enabled,
            "gate": {
                "enabled": self.gate.enabled, "active": self.gate.active,
                "gating": self.gate.gating,
                "transition_pending": self.gate.transition_pending,
                "cut_found": self.gate.cut_found,
                "candidate_seq": self.gate.candidate_seq,
                "event": self.gate.event,
            },
            "inspector": inspector,
            "audio_clock": {
                "resets": self.audio_clock.reset_count,
                "last_reset_reason": self.audio_clock.last_reset_reason,
                "last_delta_ms": self.audio_clock.last_delta_ms,
            },
            "position": {
                "position_ms": self.position.position_ms,
                "resets": self.position.reset_count,
                "monotonic": self.position.monotonic,
            },
            "counters": dict(self.counters),
            "paused": self.paused, "stopped": self.stopped,
        }

    def contract_checks(self):
        """Invariants that must hold for both normal and degraded fixtures."""
        checks = []
        releases = [t for t in self.transitions if t.released]
        checks.append({
            "name": "no_release_without_presentation_ack",
            "passed": all(t.presentation_ack and t.presentation_ack["success"]
                          for t in releases),
            "detail": f"{len(releases)} release(s) have a successful flip ack",
        })
        timeout_releases = [t for t in self.transitions
                            if t.timeout and t.released]
        checks.append({
            "name": "timeout_is_degraded_not_a_release",
            "passed": not timeout_releases,
            "detail": f"{len(timeout_releases)} timeout(s) counted as release",
        })
        route_epochs = [o for o in self.observations
                        if o["event"] == "route_changed_identity_stable"]
        checks.append({
            "name": "url_change_alone_does_not_change_epoch",
            "passed": all(o["epoch"] == o["epoch_before"]
                          for o in route_epochs),
            "detail": "route-only observations preserve the observed epoch",
        })
        checks.append({
            "name": "unsupported_controls_are_not_counted_as_tested",
            "passed": all(c["counted_as_tested"] is False
                          for c in self.controls
                          if c["status"] == "NOT IMPLEMENTED"),
            "detail": "console-only controls report NOT IMPLEMENTED",
        })
        checks.append({
            "name": "playback_position_monotonic_within_epoch",
            "passed": self.position.monotonic,
            "detail": "PTS-derived position never goes backward inside an epoch",
        })
        return checks

    def summary(self):
        return {
            "snapshot": self.snapshot(),
            "transitions": [t.as_dict() for t in self.transitions],
            "observations": list(self.observations),
            "controls": list(self.controls),
            "contract_checks": self.contract_checks(),
            "events": list(self.events),
        }


# ---------------------------------------------------------------------------
# Deterministic scenarios


def _fresh_picture(sequence):
    """Alternate 64-value luma samples to create deterministic scene cuts."""
    if sequence % 2:
        return bytes([230] * 64)
    return bytes([10] * 64)


def run_normal_sequence(transitions=1, start_pts=900000, step_pts=90000):
    """Build a fully successful sequence of source changes and releases."""
    harness = SurfAllChannelsHarness()
    harness.start(1, sync_enabled=True)
    harness.observe_source("chan-0", "boot-1", "/live/all.ts", at_ms=0)
    harness.control("play", now_ms=10)
    base = int(start_pts)
    for index in range(1, int(transitions) + 1):
        source_id = "chan-%d" % index
        boot_id = "boot-1"
        at = index * 1000
        harness.observe_source(source_id, boot_id, "/live/all.ts", at_ms=at)
        harness.tick(at + 50)
        pts = base + index * int(step_pts)
        # Feed a fresh PAT/PMT/PCR/video/audio sequence for this transition.
        data = (harness.builder.pat() + harness.builder.pmt() +
                harness.builder.pcr(pts) +
                harness.builder.video_pes(pts) +
                harness.builder.audio_pes(pts))
        harness.feed_ts(data, now_ms=at + 60)
        harness.decoded_picture(index * 2 - 1, _fresh_picture(index * 2 - 1),
                                now_ms=at + 70)
        harness.decoded_picture(index * 2, _fresh_picture(index * 2),
                                now_ms=at + 80)
        harness.queue_flip(index * 2, now_ms=at + 90)
        # Queued-only must not release; verify before completing.
        if harness.gate.gating is False:
            harness.counters["premature_releases"] += 1
        harness.flip_complete(True, index * 2, now_ms=at + 100)
    harness.control("stop", now_ms=(int(transitions) + 1) * 1000 + 150)
    return harness


def run_degraded_sequence():
    """Cover missing metadata, timeouts, stale flips, pause and resets.

    Returns a dict of independent harnesses so a failure in one degraded
    case cannot mask another.
    """
    cases = {}

    # A route/URL change without a new observed source must not bump epoch.
    url_only = SurfAllChannelsHarness()
    url_only.start(1, sync_enabled=True)
    url_only.observe_source("chan-a", "boot-a", "/live/all.ts", at_ms=0)
    url_only.observe_source("chan-a", "boot-a", "/live/sports.ts", at_ms=10)
    cases["url_only_change"] = url_only

    # No scene cut: the gate must time out rather than release audio.
    timeout = SurfAllChannelsHarness()
    timeout.start(1, sync_enabled=True)
    timeout.observe_source("chan-a", "boot-a", "/live/all.ts", at_ms=0)
    timeout.observe_source("chan-b", "boot-a", "/live/all.ts", at_ms=100)
    timeout.tick(150)
    timeout.decoded_picture(1, bytes([128] * 64), now_ms=200)
    timeout.decoded_picture(2, bytes([128] * 64), now_ms=300)
    timeout.flip_complete(True, 2, now_ms=400)
    timeout.tick(GATE_TIMEOUT_MS + 151)
    cases["no_cut_timeout"] = timeout

    # Rapid A -> B -> C: an old B candidate must not release C's gate.
    rapid = SurfAllChannelsHarness()
    rapid.start(1, sync_enabled=True)
    rapid.observe_source("chan-a", "boot-a", "/live/all.ts", at_ms=0)
    rapid.observe_source("chan-b", "boot-a", "/live/all.ts", at_ms=100)
    rapid.tick(150)
    rapid.decoded_picture(1, bytes(64), now_ms=200)
    rapid.decoded_picture(2, bytes([240] * 64), now_ms=300)
    rapid.queue_flip(2, now_ms=350)
    rapid.observe_source("chan-c", "boot-a", "/live/all.ts", at_ms=400)
    rapid.tick(450)
    rapid.decoded_picture(3, bytes(64), now_ms=500)
    rapid.decoded_picture(4, bytes([220] * 64), now_ms=600)
    rapid.flip_complete(True, 2, now_ms=650)
    rapid.flip_complete(True, 4, now_ms=700)
    cases["rapid_switch_stale_flip"] = rapid

    # Pause does not advance a new gate while paused; resume still bounds it.
    paused = SurfAllChannelsHarness()
    paused.start(1, sync_enabled=True)
    paused.observe_source("chan-a", "boot-a", "/live/all.ts", at_ms=0)
    paused.observe_source("chan-b", "boot-a", "/live/all.ts", at_ms=100)
    paused.tick(150)
    paused.control("pause", now_ms=200)
    paused.tick(GATE_TIMEOUT_MS + 141, paused=True)
    paused.control("resume", now_ms=GATE_TIMEOUT_MS + 200)
    paused.tick(GATE_TIMEOUT_MS + 151)
    cases["pause_resume_timeout"] = paused

    # PCR/PTS discontinuity and backward audio PTS reset the clock/position.
    reset = SurfAllChannelsHarness()
    reset.start(1, sync_enabled=True)
    reset.observe_source("chan-a", "boot-a", "/live/all.ts", at_ms=0)
    first = (reset.builder.pat() + reset.builder.pmt() +
             reset.builder.pcr(900000) + reset.builder.video_pes(900000) +
             reset.builder.audio_pes(900000))
    reset.feed_ts(first, now_ms=10)
    second = reset.builder.pcr(450000, discontinuity=True) + \
        reset.builder.video_pes(1000) + reset.builder.audio_pes(500)
    reset.feed_ts(second, now_ms=20)
    cases["pcr_pts_discontinuity"] = reset

    # Corrupted/sync-lost transport must be visible, not hidden as decode success.
    corrupt = SurfAllChannelsHarness()
    corrupt.start(1, sync_enabled=True)
    corrupt.observe_source("chan-a", "boot-a", "/live/all.ts", at_ms=0)
    corrupt.feed_ts(corrupt.builder.pat() + corrupt.builder.pmt(),
                    now_ms=10)
    corrupt.feed_ts(b"\x00\x00" + corrupt.builder.corrupt_packet(),
                    now_ms=20)
    cases["corrupt_transport"] = corrupt

    # Disabled sync mode observes the source change but starts no gate.
    disabled = SurfAllChannelsHarness()
    disabled.start(1, sync_enabled=False)
    disabled.observe_source("chan-a", "boot-a", "/live/all.ts", at_ms=0)
    disabled.observe_source("chan-b", "boot-a", "/live/all.ts", at_ms=100)
    disabled.tick(150)
    cases["sync_disabled"] = disabled

    return cases


def run_missing_controls_sequence():
    """Explicitly record every console control the desktop harness lacks."""
    harness = SurfAllChannelsHarness()
    harness.start(2, sync_enabled=True)
    harness.observe_source("chan-a", "boot-a", "/live/all.ts")
    for offset, name in enumerate(sorted(UNSUPPORTED_CONTROLS)):
        harness.control(name, now_ms=offset * 10)
    # A live seek is tested and rejected; it is not silently accepted.
    harness.control("seek", now_ms=500)
    return harness


def _scenario_summary(harness):
    return {
        "snapshot": harness.snapshot(),
        "transitions": [t.as_dict() for t in harness.transitions],
        "controls": list(harness.controls),
        "contract_checks": harness.contract_checks(),
        "counters": dict(harness.counters),
    }


def measurement_report(transitions=100):
    """JSON-serializable deterministic measurement and contract report.

    All timing values are synthetic/modeled.  The report never promotes a
    host fixture to physical console evidence.
    """
    normal = run_normal_sequence(transitions=transitions)
    degraded = run_degraded_sequence()
    missing = run_missing_controls_sequence()
    report = {
        "schema": SCHEMA,
        "reference": REFERENCE,
        "metric_source": "synthetic_modeled_fixture",
        "physical": False,
        "hardware_emulator": False,
        "live_server_calls": False,
        "actual_media": False,
        "units": {
            "at_ms": "milliseconds (synthetic virtual time)",
            "signed_offset_ms": "milliseconds, video PTS minus audio PTS",
            "position_ms": "milliseconds, PTS/90 derived",
            "pts_ticks": "90 kHz MPEG-TS PTS ticks",
        },
        "capabilities": {
            "desktop_controls": DESKTOP_CONTROLS,
            "unsupported_controls": UNSUPPORTED_CONTROLS,
            "unavailable_measurements": UNAVAILABLE_MEASUREMENTS,
        },
        "normal": _scenario_summary(normal),
        "degraded": {
            name: _scenario_summary(case) for name, case in degraded.items()
        },
        "missing_controls": _scenario_summary(missing),
        "totals": {
            "normal_transitions": len(normal.transitions),
            "normal_releases": normal.counters["audio_releases"],
            "normal_timeouts": normal.counters["timeouts"],
            "degraded_cases": len(degraded),
            "degraded_timeouts": sum(
                case.counters["timeouts"] for case in degraded.values()),
            "not_implemented_controls": missing.counters[
                "not_implemented_controls"],
            "fixture_transitions": len(normal.transitions) + sum(
                len(case.transitions) for case in degraded.values()),
        },
    }
    return report


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    transitions = 100
    if "--transitions" in argv:
        try:
            transitions = max(1, int(argv[argv.index("--transitions") + 1]))
        except (ValueError, IndexError):
            print("usage: python -m ps3lab.surf_all_channels "
                  "[--transitions N]", file=sys.stderr)
            return 2
    report = measurement_report(transitions=transitions)
    print(json.dumps(report, indent=2, sort_keys=True))
    checks = report["normal"]["contract_checks"]
    return 0 if all(check["passed"] for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
