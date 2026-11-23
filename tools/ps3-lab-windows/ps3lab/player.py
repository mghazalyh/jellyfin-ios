"""Real mpv playback embedding and private JSON IPC.

Uses the Windows HWND video host and native mpv named pipes. The media URL
is a random, opaque localhost relay address; remote credentials stay private.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import uuid


def new_ipc_path() -> str:
    identifier = "jellyfin-ps3lab-" + uuid.uuid4().hex
    if os.name == "nt":
        return "\\\\.\\pipe\\" + identifier
    return "/tmp/" + identifier + ".sock"


def build_mpv_args(executable: str, url: str, window_id: int, ipc_path: str) -> list[str]:
    if not executable or not url.startswith("http://127.0.0.1:"):
        raise ValueError("mpv requires an executable and a private loopback URL")
    if not isinstance(window_id, int) or window_id <= 0:
        raise ValueError("video host must have a valid native window handle")
    if not ipc_path:
        raise ValueError("mpv requires a unique IPC endpoint")
    handle = window_id & 0xffffffff if os.name == "nt" else window_id
    return [
        executable, "--no-config", "--no-terminal", "--force-window=immediate",
        "--keep-open=no", "--hwdec=auto-safe", "--cache=yes", "--cache-secs=3",
        "--osc=no", "--input-default-bindings=no", "--input-vo-keyboard=no",
        "--cursor-autohide=always", "--idle=no", "--audio-display=no",
        "--title=Jellyfin PS3 Stream", "--wid=" + str(handle),
        "--input-ipc-server=" + ipc_path, url,
    ]


def encode_command(parts: list, request_id: int | None = None) -> bytes:
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], str):
        raise ValueError("invalid mpv IPC command")
    req = {"command": parts}
    if request_id is not None:
        req["request_id"] = request_id
    return (json.dumps(req, separators=(",", ":")) + "\n").encode("utf-8")


def ipc_command(path: str, parts: list) -> dict:
    """Send a command on a worker and ignore asynchronous mpv event messages.

    Clients connecting during mpv startup may briefly see a missing pipe.
    Retry the connection, then require the matching request_id response.
    """
    if not path:
        raise RuntimeError("mpv IPC is not active")
    request_id = secrets.randbelow(2**30) + 1
    payload = encode_command(parts, request_id)
    last_error = None
    for attempt in range(12):
        try:
            with open(path, "r+b", buffering=0) as connection:
                connection.write(payload)
                for _ in range(80):
                    message = connection.readline(65536)
                    if not message:
                        raise RuntimeError("mpv closed the IPC channel")
                    try:
                        reply = json.loads(message.decode("utf-8"))
                    except (ValueError, UnicodeError):
                        continue
                    if reply.get("request_id") != request_id:
                        continue
                    if reply.get("error") != "success":
                        raise RuntimeError("mpv command rejected: " +
                                           str(reply.get("error")))
                    return reply
                raise RuntimeError("mpv did not acknowledge the command")
        except OSError as exc:
            last_error = exc
            if attempt == 11:
                break
            time.sleep(0.1)
    raise RuntimeError("mpv pipe unavailable (" +
                       type(last_error).__name__ + ")")


def validated_volume(value) -> int:
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError, OverflowError):
        return 80


def validated_seek(value) -> int:
    try:
        return max(-300, min(300, int(float(value))))
    except (TypeError, ValueError, OverflowError):
        return 0
