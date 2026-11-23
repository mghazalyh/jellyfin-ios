"""Real mpv playback embedding and private JSON IPC.

Uses the Windows HWND video host and native mpv named pipes. The media URL
is a random, opaque localhost relay address; remote credentials stay private.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import time
import uuid


def new_ipc_path() -> str:
    identifier = "jellyfin-ps3lab-" + uuid.uuid4().hex
    if os.name == "nt":
        return "\\\\.\\pipe\\" + identifier
    return "/tmp/" + identifier + ".sock"


def build_mpv_args(executable: str, url: str, window_id: int, ipc_path: str,
                   platform: str | None = None) -> list[str]:
    if not executable or not url.startswith("http://127.0.0.1:"):
        raise ValueError("mpv requires an executable and a private loopback URL")
    platform = sys.platform if platform is None else platform
    is_mac = platform == "darwin"
    if not is_mac and (not isinstance(window_id, int) or window_id <= 0):
        raise ValueError("video host must have a valid native window handle")
    if not ipc_path:
        raise ValueError("mpv requires a unique IPC endpoint")
    handle = window_id & 0xffffffff if platform == "win32" else window_id
    args = [
        executable, "--no-config", "--no-terminal", "--force-window=immediate",
        "--keep-open=no", "--hwdec=auto-safe", "--cache=yes", "--cache-secs=3",
        "--osc=no", "--input-default-bindings=no", "--input-vo-keyboard=no",
        "--cursor-autohide=always", "--idle=no", "--audio-display=no",
        "--title=Jellyfin PS3 Stream",
        "--input-ipc-server=" + ipc_path,
    ]
    if is_mac:
        # Cocoa cannot re-parent foreign processes into the tkinter NSView.
        # Use an actual native mpv window with its own playback controls.
        args.extend(["--osc=yes", "--input-vo-keyboard=yes",
                     "--ontop=no", "--vo=gpu-next"])
    else:
        args.append("--wid=" + str(handle))
    args.append(url)
    return args


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
            if os.name == "nt":
                # Windows mpv is a named pipe; Python's file IO is correct.
                connection = open(path, "r+b", buffering=0)
                try:
                    connection.write(payload)
                    for _ in range(80):
                        message = connection.readline(65536)
                        if not message:
                            raise RuntimeError("mpv closed the IPC channel")
                        try:
                            reply = json.loads(message.decode("utf-8"))
                        except (ValueError, UnicodeError):
                            continue
                        if reply.get("request_id") == request_id:
                            if reply.get("error") != "success":
                                raise RuntimeError("mpv command rejected: " +
                                                   str(reply.get("error")))
                            return reply
                    raise RuntimeError("mpv did not acknowledge the command")
                finally:
                    connection.close()
            else:
                # mpv IPC on macOS/Linux is an AF_UNIX SOCKET, not a regular
                # file. open("r+b") would raise ENXIO on a socket inode.
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(4.0)
                    client.connect(path)
                    client.sendall(payload)
                    with client.makefile("rb") as stream:
                        for _ in range(80):
                            message = stream.readline(65536)
                            if not message:
                                raise RuntimeError("mpv closed the IPC socket")
                            try:
                                reply = json.loads(message.decode("utf-8"))
                            except (ValueError, UnicodeError):
                                continue
                            if reply.get("request_id") == request_id:
                                if reply.get("error") != "success":
                                    raise RuntimeError("mpv command rejected: " +
                                                       str(reply.get("error")))
                                return reply
                        raise RuntimeError("mpv did not acknowledge the command")
        except (OSError, TimeoutError) as exc:
            last_error = exc
            if attempt == 11:
                break
            time.sleep(0.1)
    raise RuntimeError("mpv IPC unavailable (" +
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
