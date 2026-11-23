"""Production-Jellyfin PS3 client readiness checks with redacted evidence.

Requires no credentials for public connectivity. Authenticated read-only checks
need JELLYFIN_TEST_TOKEN or JELLYFIN_TEST_USERNAME/PASSWORD in the process
environment. Actual media streaming and session writes require --stream-test.
Never prints tokens, library names, media IDs, URLs, or user data.

Run only against an explicitly authorized server. The Windows Jellyfin server
is not modified; playstate test requests can update the disposable test user's
watch history, which is why --stream-test is opt-in.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ps3lab.jellyfin import JellyfinClient, JellyfinError
from ps3lab.model import Profile
from ps3lab.proxy import LocalProxy, StreamController

MAX_REPORT_DETAIL = 120
CATEGORIES = ("home", "movies", "shows", "resume", "live")


@dataclass
class Checks:
    public_ok: bool = False
    authentication_ok: bool = False
    library_ok: bool = False
    streaming_ok: bool = False
    seek_ok: bool = False
    seek_requested: bool = False
    lifecycle_ok: bool = False
    live_checked: bool = False
    completed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    server_version: str = "unknown"
    category_counts: dict = field(default_factory=dict)
    transport_packets: int = 0
    selected_type: str = ""

    def success(self, test_stream: bool, has_credentials: bool) -> bool:
        if not self.public_ok or self.failures:
            return False
        if not has_credentials:
            return not test_stream
        if not (self.authentication_ok and self.library_ok):
            return False
        if test_stream:
            return self.streaming_ok and self.lifecycle_ok and (
                self.seek_ok if self.seek_requested and self.selected_type != "TvChannel"
                else True)
        return True

    def as_dict(self, test_stream: bool, has_credentials: bool) -> dict:
        return {
            "schema": "jellyfin-ps3-real-server-gate/v1",
            "passed": self.success(test_stream, has_credentials),
            "scope": ("authenticated-streaming" if test_stream else
                      "authenticated-readonly" if has_credentials else "public-health"),
            "server_version": self.server_version,
            "checks": {
                "public": self.public_ok, "authentication": self.authentication_ok,
                "library": self.library_ok, "streaming": self.streaming_ok,
                "session_seek": self.seek_ok, "seek_requested": self.seek_requested,
                "playstate": self.lifecycle_ok, "live_checked": self.live_checked,
            },
            "category_counts": self.category_counts,
            "transport_packets": self.transport_packets,
            "selected_type": self.selected_type,
            "completed": list(self.completed),
            "skipped": list(self.skipped),
            "failures": list(self.failures),
        }

    def fail(self, phase: str, exc: BaseException):
        # Never stringify an exception: server plugins can echo query strings,
        # tokens, file paths or actual media/user names into exception text.
        self.failures.append({"phase": phase, "error_type": type(exc).__name__})


def safe_public_info(server: str, timeout: float = 8.0) -> dict:
    parsed = urllib.parse.urlsplit(server)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Invalid scheme or hostname")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Server URL must not contain credentials")
    if parsed.path not in ("", "/"):
        raise ValueError("Server URL must be an origin, without a path")
    url = server.rstrip("/") + "/System/Info/Public"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError("Unexpected public status")
        raw = response.read(65536 + 1)
        if len(raw) > 65536:
            raise RuntimeError("Oversized Jellyfin public response")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("Invalid public status data")
        return data


def available_test_credentials() -> bool:
    return bool(
        os.environ.get("JELLYFIN_TEST_TOKEN", "").strip() or
        (os.environ.get("JELLYFIN_TEST_USERNAME", "").strip() and
         os.environ.get("JELLYFIN_TEST_PASSWORD", ""))
    )


def login(client: JellyfinClient) -> None:
    token = os.environ.get("JELLYFIN_TEST_TOKEN", "").strip()
    if token:
        client.authenticate_token(token)
        return
    username = os.environ.get("JELLYFIN_TEST_USERNAME", "").strip()
    password = os.environ.get("JELLYFIN_TEST_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("No supplied test credentials")
    client.authenticate(username, password)


def check_network(server: str, result: Checks) -> None:
    started = time.monotonic()
    info = safe_public_info(server)
    version = info.get("Version") or "unknown"
    result.server_version = str(version)[:MAX_REPORT_DETAIL]
    result.public_ok = True
    result.completed.append("HTTP /System/Info/Public 200")
    # Timing is retained internally, but no source IP or server identity
    # is logged in permanent test artifacts.
    _ = round((time.monotonic() - started) * 1000)


def check_library(client: JellyfinClient, result: Checks) -> dict:
    pages: dict = {}
    failures = 0
    for category in CATEGORIES:
        try:
            page = client.browse(category, start=0, limit=10)
            if not isinstance(page.get("Items"), list):
                raise RuntimeError("Invalid list")
            pages[category] = page
            result.category_counts[category] = min(1000000, max(
                0, int(page.get("TotalRecordCount") or 0)))
        except Exception as exc:
            failures += 1
            if category == "live":
                # Jellyfin installations can legitimately lack Live TV.
                result.skipped.append("live-tv-not-configured-or-inaccessible")
            else:
                result.fail("browse-" + category, exc)
    result.library_ok = failures == 0 or (
        failures == 1 and "live-tv-not-configured-or-inaccessible" in result.skipped)
    if result.library_ok:
        result.completed.append("Authenticated PS3 library queries")
    return pages


def choose_item(pages: dict, desired: str = "") -> dict | None:
    ordered = (("live",) if desired == "live" else
               ("movies", "home", "resume") if desired == "vod" else
               ("movies", "home", "resume", "live"))
    for category in ordered:
        for item in (pages.get(category, {}).get("Items") or []):
            if isinstance(item, dict) and item.get("Id") and item.get("Type") in (
                "Movie", "Episode", "TvChannel"):
                return item
    return None


def read_transport(client: JellyfinClient, session, result: Checks,
                   target_packets=256, max_seconds=35.0) -> None:
    """Open real Jellyfin MPEG-TS through the PS3 loopback inspection relay."""
    events: list = []
    ctl = StreamController(lambda event, values: events.append(
        (str(event)[:30], type(values).__name__)))
    ctl.configure(session.url, client.headers(), Profile.from_switches(False, False))
    proxy = LocalProxy(ctl)
    try:
        proxy.start()
        with urllib.request.urlopen(proxy.url, timeout=max_seconds) as response:
            total = 0
            deadline = time.monotonic() + max_seconds
            while total < target_packets * 188 and time.monotonic() < deadline:
                block = response.read(min(188 * 32, target_packets * 188 - total))
                if not block:
                    break
                total += len(block)
        observed = ctl.inspector.snapshot()
        packets = int(observed.get("ts_packets") or 0)
        result.transport_packets = max(result.transport_packets, packets)
        if packets < 16:
            raise RuntimeError("Insufficient MPEG-TS packets")
        result.completed.append("Real PS3 identity MPEG-TS relay and packet inspection")
    finally:
        try:
            proxy.close()
        except (OSError, RuntimeError):
            pass


def run_stream_tests(client: JellyfinClient, item: dict,
                     result: Checks, test_seek: bool) -> None:
    """Opt-in real authenticated stream; always attempt the stopped report."""
    session = None
    begun = False
    media_type = str(item.get("Type") or "")[:30]
    result.selected_type = media_type
    try:
        session = client.make_session(
            item, hd=False, surround=False, quality="720p",
            budget_audio=True, no_video_copy=True)
        client.playstate(session, "start", pos_ticks=0)
        begun = True
        read_transport(client, session, result)
        result.streaming_ok = True
        client.playstate(
            session, "progress",
            pos_ticks=0 if session.live else max(0, int(session.start_ticks)),
            paused=False)
        result.lifecycle_ok = True
        if session.live:
            result.live_checked = True
            result.seek_ok = True
            result.skipped.append("live-tv-is-not-seekable")
        elif test_seek:
            duration = int(item.get("RunTimeTicks") or 0)
            ticks = min(150000000, max(0, duration // 5)) if duration else 150000000
            seek_session = client.seek_session(
                session, ticks, quality="720p", no_video_copy=True)
            if seek_session.play_session_id != session.play_session_id:
                raise RuntimeError("Session continuity broken")
            if seek_session.source_id != session.source_id:
                raise RuntimeError("Media source changed during seek")
            read_transport(client, seek_session, result,
                           target_packets=64, max_seconds=45)
            result.seek_ok = True
            result.completed.append("PlaySessionId/source continuity over seek")
        else:
            # A skipped optional seek must never be reported as verified.
            result.seek_ok = False
            result.skipped.append("seek-not-requested")
    except Exception as exc:
        result.fail("authenticated-stream", exc)
    finally:
        if session and begun:
            try:
                client.playstate(
                    session, "stopped",
                    pos_ticks=0 if session.live else max(0, int(session.start_ticks)),
                    paused=False)
                result.completed.append("Stopped session reported to Jellyfin")
            except Exception as exc:
                result.lifecycle_ok = False
                result.fail("playstate-stop", exc)


def execute(server: str, stream_test: bool, test_seek: bool,
            media: str) -> dict:
    checks = Checks(seek_requested=test_seek)
    authenticated = available_test_credentials()
    try:
        check_network(server, checks)
    except Exception as exc:
        checks.fail("public-connectivity", exc)
        return checks.as_dict(stream_test, authenticated)
    if not authenticated:
        checks.skipped.append("no-JELLYFIN_TEST_TOKEN-or-test-user-credentials")
        if stream_test:
            checks.fail("authentication-required", RuntimeError("No credentials"))
        return checks.as_dict(stream_test, authenticated)
    try:
        client = JellyfinClient(server)
        login(client)
        checks.authentication_ok = True
        checks.completed.append("PS3 MediaBrowser identity authentication")
    except Exception as exc:
        checks.fail("authentication", exc)
        return checks.as_dict(stream_test, authenticated)
    pages = check_library(client, checks)
    if not stream_test:
        checks.skipped.append("real-media-test-opt-in-required")
        return checks.as_dict(stream_test, authenticated)
    if not checks.library_ok:
        checks.skipped.append("media-tests-blocked-by-library-errors")
        return checks.as_dict(stream_test, authenticated)
    item = choose_item(pages, media)
    if item is None:
        checks.skipped.append("no-accessible-playable-test-media")
        checks.fail("media-selection", RuntimeError("No playable item"))
        return checks.as_dict(stream_test, authenticated)
    run_stream_tests(client, item, checks, test_seek)
    return checks.as_dict(stream_test, authenticated)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PS3 wire protocol and Jellyfin server live readiness gate")
    parser.add_argument("--server", required=True, help="Jellyfin origin URL")
    parser.add_argument("--stream-test", action="store_true",
                        help="Opt in to real streams and test-user playstate updates")
    parser.add_argument("--seek", action="store_true",
                        help="Verify server-side VOD seek with retained session ID")
    parser.add_argument("--media", choices=("auto", "vod", "live"), default="vod",
                        help="Prefer a short representative VOD or Live TV item")
    parser.add_argument("--report", default="",
                        help="Optional path for redacted machine-readable result")
    args = parser.parse_args(argv)
    try:
        result = execute(args.server, args.stream_test, args.seek,
                         args.media)
    except Exception as exc:
        result = {
            "schema": "jellyfin-ps3-real-server-gate/v1", "passed": False,
            "failures": [{"phase": "internal", "error_type": type(exc).__name__}]
        }
    serialized = json.dumps(result, indent=2, sort_keys=True)
    print(serialized)
    if args.report:
        try:
            Path(args.report).write_text(serialized + "\n", encoding="utf-8")
        except OSError:
            return 3
    return 0 if result.get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
