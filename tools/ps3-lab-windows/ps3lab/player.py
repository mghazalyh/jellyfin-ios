"""Native video embedding and JSON IPC for the bundled real mpv player.

RPCS3 is the separate execution engine for PS3 instructions. This module
only decodes and renders the PS3-negotiated Jellyfin MPEG-TS stream on Windows.
"""
from __future__ import annotations

import json
import os
import uuid


def new_ipc_path() -> str:
    name = "jellyfin-ps3lab-" + uuid.uuid4().hex
    if os.name == "nt":
        return "\\\\.\\pipe\\" + name
    return "/tmp/" + name + ".sock"


def build_mpv_args(executable: str, url: str, window_id: int, ipc_path: str) -> list[str]:
    if not executable or not url.startswith("http://127.0.0.1:"):
        raise ValueError("mpv requires an executable and the opaque loopback media URL")
    if not isinstance(window_id, int) or window_id <= 0:
        raise ValueError("video host must have an active native window ID")
    if not ipc_path:
        raise ValueError("IPC socket/pipe is required")
    handle = window_id & 0xffffffff if os.name == "nt" else window_id
    return [
        executable, "--no-config", "--no-terminal", "--force-window=immediate",
        "--keep-open=no", "--hwdec=auto-safe", "--cache=yes", "--cache-secs=2",
        "--osc=no", "--input-default-bindings=no", "--input-vo-keyboard=no",
        "--cursor-autohide=always", "--idle=no", "--audio-display=no",
        "--title=Jellyfin PS3 Theater", "--wid=" + str(handle),
        "--input-ipc-server=" + ipc_path, url,
    ]


def encode_command(parts: list) -> bytes:
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], str):
        raise ValueError("invalid mpv command")
    return (json.dumps({"command": parts}, separators=(",", ":")) + "\n").encode("utf-8")


def ipc_command(path: str, parts: list) -> dict:
    """Open a short-lived private mpv connection on a background worker.

    Do not call from the Tk event thread. No credentials or upstream URLs
    are ever exposed over mpv IPC. Only the opaque local transport is played.
    """
    if not path:
        raise RuntimeError("mpv IPC is not active")
    payload = encode_command(parts)
    try:
        with open(path, "r+b", buffering=0) as connection:
            connection.write(payload)
            answer = connection.readline(65536)
            if not answer:
                raise RuntimeError("mpv IPC returned no data")
            response = json.loads(answer.decode("utf-8"))
            if response.get("error") not in ("success", None):
                raise RuntimeError("mpv command rejected: " + str(response.get("error")))
            return response
    except (OSError, ValueError, UnicodeError) as exc:
        raise RuntimeError("mpv IPC unavailable (" + type(exc).__name__ + ")") from exc


def validated_volume(value) -> int:
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError, OverflowError):
        return 80


def validated_seek(value) -> int:
    try:
        return max(-300, min(300, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 0
