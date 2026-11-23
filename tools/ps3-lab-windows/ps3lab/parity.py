"""Pure request parity with JellyFin-PS3 v3.6.5-surf.1.

References:
  source/util/vquality.cpp
  source/player/core/stream_budget.h
  source/player/stream/stream_request.cpp
  source/player/core/player_session.cpp

These calculations are host-testable and never depend on PS3 hardware.
"""
from __future__ import annotations
from dataclasses import dataclass

# The retired original/30M modes remain legal to test and replay older logs,
# but they are deliberately omitted from selectable quality controls.
QUALITY_ORDER = ("auto", "360p", "480p", "720p", "1080p",
                 "1080p20", "1080p25")
QUALITY_LEVELS = {
    "360p": (640, 360, "baseline", "31", 700000),
    "480p": (854, 480, "baseline", "31", 1500000),
    "720p": (1280, 720, "baseline", "31", 4000000),
    "1080p": (1920, 1080, "high", "42", 10000000),
    "1080p20": (1920, 1080, "high", "42", 20000000),
    "1080p25": (1920, 1080, "high", "42", 25000000),
    "1080p30-retired": (1920, 1080, "high", "42", 30000000),
    "original-retired": (1920, 1080, "high", "42", 0),
}
TS_OVERHEAD_PERCENT = 3
AUDIO_EST_DTS_HD_BPS = 3000000
AUDIO_EST_TRUEHD_BPS = 4500000


@dataclass(frozen=True)
class VideoMode:
    quality: str
    width: int
    height: int
    profile: str
    level: str
    step_bps: int


def video_mode(quality="auto", hd=False, live=False,
               display_width=0, display_height=0) -> VideoMode:
    mode = str(quality or "auto").lower()
    if mode == "auto":
        mode = "1080p" if hd else "720p"
    if mode not in QUALITY_LEVELS:
        raise ValueError("unknown PS3 video quality")
    w, h, codec, level, bitrate = QUALITY_LEVELS[mode]
    if live:
        codec = "high"
        level = "42" if level == "42" else "41"
        if bitrate == 0:
            bitrate = 10000000
    if not mode.startswith("1080p") and mode != "original-retired":
        if display_width and display_width > 0:
            w = min(w, int(display_width))
        if display_height and display_height > 0:
            h = min(h, int(display_height))
    return VideoMode(mode, w, h, codec, level, bitrate)


def audio_cost(codec="mp3", reported_bps=0):
    raw = max(0, int(reported_bps or 0))
    if codec == "dts":
        return max(raw, AUDIO_EST_DTS_HD_BPS)
    if codec == "truehd":
        return max(raw, AUDIO_EST_TRUEHD_BPS)
    if codec in ("mp3", "ac3"):
        return raw
    raise ValueError("unsupported PS3 audio codec")


def video_budget(step_bps: int, audio_bps: int) -> int:
    step = max(0, int(step_bps))
    audio = max(0, int(audio_bps))
    if not step:
        return 0
    ceiling = step * (100 - TS_OVERHEAD_PERCENT) // 100
    floor = step * 50 // 100
    return max(floor, ceiling - audio)


def audio_params(surround=False, hd_codec="", ac3_copy=False):
    """Exact fields emitted by stream_request.cpp for selected audio mode."""
    if ac3_copy:
        return [
            ("AudioCodec", "ac3"),
            ("AudioSampleRate", 48000),
            ("MaxAudioChannels", 6),
        ], True, "ac3"
    if hd_codec:
        codec = str(hd_codec).lower()
        if codec not in ("dts", "truehd"):
            raise ValueError("HD audio codec must be dts or truehd")
        return [
            ("AudioCodec", "ac3," + codec + ",mp3"),
            ("AudioSampleRate", 48000),
            ("MaxAudioChannels", 8),
        ], True, codec
    codec = "ac3" if surround else "mp3"
    return [
        ("AudioCodec", codec),
        ("AudioBitrate", 640000 if surround else 192000),
        ("AudioSampleRate", 48000),
        ("MaxAudioChannels", 6 if surround else 2),
    ], False, codec


def stream_query(
    *,
    quality="auto", hd=False, live=False, surround=False,
    hd_codec="", ac3_copy=False, reported_audio_bps=0,
    budget_audio=True, no_video_copy=False,
    display_width=0, display_height=0,
    device_id="", source_id="", start_ticks=0,
    live_stream_id="", play_session_id="",
    audio_index=-1, subtitle_index=-1, subtitle_on_console=False,
):
    if not device_id or not source_id:
        raise ValueError("device and media source are required")
    if int(start_ticks) < 0:
        raise ValueError("start_ticks must be non-negative")
    mode = video_mode(quality, hd, live, display_width, display_height)
    audio, audio_copy, codec = audio_params(surround, hd_codec, ac3_copy)
    if hd_codec:
        cost = audio_cost(codec, reported_audio_bps)
    elif ac3_copy and reported_audio_bps:
        cost = audio_cost("ac3", reported_audio_bps)
    else:
        cost = audio_cost(codec, 640000 if surround or ac3_copy else 192000)
    step = mode.step_bps
    requested_video = video_budget(step, cost) if budget_audio else step
    opts = [
        ("VideoCodec", "h264"),
        ("Profile", mode.profile),
        ("Level", mode.level),
        ("MaxWidth", mode.width),
        ("MaxHeight", mode.height),
    ]
    if requested_video:
        opts.append(("VideoBitrate", requested_video))
    opts.append(("AllowVideoStreamCopy", "false" if live or no_video_copy else "true"))
    opts.extend(audio)
    opts.extend([
        ("MaxFramerate", 30),
        ("AllowAudioStreamCopy", "true" if audio_copy else "false"),
        ("DeviceId", device_id),
        ("Static", "false"),
        ("MediaSourceId", source_id),
        ("StartTimeTicks", int(start_ticks)),
    ])
    if live_stream_id:
        opts.append(("LiveStreamId", live_stream_id))
    if int(audio_index) >= 0:
        opts.append(("AudioStreamIndex", int(audio_index)))
    if int(subtitle_index) >= 0 and not subtitle_on_console:
        opts.extend([("SubtitleStreamIndex", int(subtitle_index)),
                     ("SubtitleMethod", "Encode")])
    if play_session_id:
        opts.append(("PlaySessionId", play_session_id))
    return opts
