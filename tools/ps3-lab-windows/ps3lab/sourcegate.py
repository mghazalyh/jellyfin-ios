"""Exact logical transitions of the opt-in Surf PCM gate in PS3 v3.6.5.

Reference source/player/surf_sync.cpp, surf_gate_logic.h.
Caller must provide an OBSERVED server source change, two 64-point decoded
luma samples and a COMPLETED (not merely queued) RSX flip. Unverified
iPad/Windows video-ready events cannot be presented as physical RSX evidence.
"""
from __future__ import annotations
from dataclasses import dataclass

def is_scene_cut(previous: bytes | None, current: bytes | None) -> tuple[bool, int, int]:
    if not previous or not current or len(previous) != 64 or len(current) != 64:
        return False, 0, 0
    diffs = [abs(a - b) for a, b in zip(previous, current)]
    avg = sum(diffs) // 64
    changed = sum(1 for value in diffs if value >= 34)
    return avg >= 27 and changed * 2 >= 64, avg, changed

def sequence_reached(presented: int, target: int) -> bool:
    # Match (int32_t)(presented - candidate) >= 0 with wraparound.
    return ((presented - target) & 0xffffffff) < 0x80000000

@dataclass
class SurfAudioGate:
    enabled: bool = False
    active: bool = False
    generation: int = 0
    epoch: int = 0
    source: tuple[str, str] | None = None
    transition_pending: bool = False
    gating: bool = False
    last_luma: bytes | None = None
    cut_found: bool = False
    candidate_seq: int = 0
    last_luma_seq: int | None = None
    gate_started_ms: int = 0
    event: str = "idle"
    timeout_ms: int = 3000

    def session(self, generation: int, active=True):
        self.active = bool(active)
        self.enabled = False
        self.generation = generation
        self.epoch = 0
        self.source = None
        self.transition_pending = False
        self.gating = False
        self.last_luma = None
        self.cut_found = False
        self.candidate_seq = 0
        self.last_luma_seq = None
        self.event = "session_started"

    def set_enabled(self, enabled: bool):
        self.enabled = self.active and enabled
        self.transition_pending = False
        self.gating = False
        self.cut_found = False
        self.event = "sync_beta_enabled" if self.enabled else "sync_beta_disabled"

    def observe(self, generation: int, source_id: str, boot: str) -> str:
        if not self.active or generation != self.generation or not source_id:
            return "ignored"
        source = (source_id, boot or "")
        if self.source is None:
            self.source = source
            self.event = "initial_source"
            return self.event
        if source != self.source:
            self.source = source
            self.epoch += 1
            self.transition_pending = self.enabled
            self.cut_found = False
            self.event = "transition_observed" if self.enabled else "observed_sync_disabled"
            return self.event
        return "unchanged"

    def tick(self, now_ms: int, paused=False, passthrough=False) -> str:
        if not self.active or not self.enabled or paused:
            return "unchanged"
        if self.transition_pending:
            self.transition_pending = False
            self.gating = not passthrough
            self.gate_started_ms = now_ms
            self.cut_found = False
            self.last_luma = None
            self.last_luma_seq = None
            self.event = "audio_gate_started" if self.gating else "bitstream_gate_skipped"
            return self.event
        if self.gating and now_ms - self.gate_started_ms >= self.timeout_ms:
            self.gating = False
            self.event = "gate_timeout_recovery"
            return self.event
        return "unchanged"

    def decoded_picture(self, sequence: int, luma_8x8: bytes) -> str:
        if not self.active or not self.enabled or self.last_luma_seq == sequence:
            return "unchanged"
        if len(luma_8x8) != 64:
            raise ValueError("expected 64 sampled luma values")
        if self.gating and self.last_luma is not None and not self.cut_found:
            cut, _avg, _changed = is_scene_cut(self.last_luma, luma_8x8)
            if cut:
                self.cut_found = True
                self.candidate_seq = sequence
                self.event = "visual_cut_decoded"
        self.last_luma = bytes(luma_8x8)
        self.last_luma_seq = sequence
        return self.event if self.event == "visual_cut_decoded" else "unchanged"

    def flip_complete(self, success: bool, sequence: int) -> str:
        if success and self.active and self.enabled and self.gating and self.cut_found:
            if sequence_reached(sequence, self.candidate_seq):
                self.gating = False
                self.event = "video_flip_audio_release"
                return self.event
        return "unchanged"
