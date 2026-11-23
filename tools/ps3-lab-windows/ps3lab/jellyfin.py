"""Jellyfin requests shaped to match the installed PS3 v3.6.5 Surf client.

The emulator's DeviceId is deliberately unique, NEVER the PS3's own ID:
reusing the physical PS3 DeviceId can revoke its authorization token.
The Client and Device strings and Video stream query mirror the PS3 fork.
"""
from __future__ import annotations
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

PS3_USER_AGENT = "JellyfinPS3/0.1"
PS3_CLIENT = "PS3"
PS3_DEVICE = "PS3"
PS3_VERSION = "0.1"

class JellyfinError(RuntimeError):
    pass

@dataclass
class PlaybackSession:
    item_id: str
    source_id: str
    play_session_id: str
    live_stream_id: str
    live: bool
    title: str
    url: str

def ps3_device_profile(hd: bool = False, surround: bool = False) -> dict:
    """Match the default 720p stereo body_sd of api_detail.cpp.

    The 1080p and AC-3 variants use the same exact request structure but
    higher codec ceiling. Further HD-audio variants are intentionally not
    asserted equivalent until their matching fork blobs are regression-tested.
    """
    width, height, ceiling, level = (1920, 1080, 10000000, "42") if hd else (
        1280, 720, 8000000, "31"
    )
    audio_codec, channels, audio_bitrate = ("ac3", 6, 640000) if surround else (
        "mp3", 2, 192000
    )
    profile = "high|main|baseline" if hd else "baseline"
    def condition(property_name: str, value: str, operator="LessThanEqual", required=True):
        return {"Condition": operator, "Property": property_name, "Value": value,
                "IsRequired": required}
    return {
        "Name": "PS3", "MaxStreamingBitrate": ceiling,
        "MaxStaticBitrate": ceiling, "MusicStreamingTranscodingBitrate": 192000,
        "DirectPlayProfiles": [],
        "TranscodingProfiles": [{
            "Type": "Video", "Container": "ts", "VideoCodec": "h264",
            "AudioCodec": audio_codec, "Protocol": "http",
            "Context": "Streaming", "MaxAudioChannels": str(channels)
        }],
        "CodecProfiles": [
            {"Type": "Video", "Codec": "h264", "Conditions": [
                condition("VideoProfile", profile, "EqualsAny"),
                condition("VideoLevel", level),
                condition("Width", str(width)),
                condition("Height", str(height)),
                condition("VideoBitrate", str(10000000 if hd else 4000000))
            ]},
            {"Type": "VideoAudio", "Codec": audio_codec, "Conditions": [
                condition("AudioChannels", str(channels), required=False),
                condition("AudioSampleRate", "48000", "Equals", required=False)
            ]}
        ],
        "ContainerProfiles": [],
        "SubtitleProfiles": [
            {"Format": fmt, "Method": "External"} for fmt in (
                "subrip", "srt", "ass", "ssa", "vtt"
            )
        ] + [
            {"Format": "pgssub", "Method": "Encode"},
            {"Format": "dvdsub", "Method": "Encode"}
        ]
    }

def build_stream_url(base: str, item_id: str, source_id: str, device_id: str,
                     play_session_id: str = "", live_stream_id: str = "",
                     live: bool = False, hd: bool = False, surround: bool = False,
                     start_ticks: int = 0, quality_bps: int = 4000000) -> str:
    """Preserve ordering / semantics in stream_request.cpp lines 179-206.

    Live requests always disable video stream copy. Transcoding profile High
    is specifically used for Live TV; the 720p default is baseline otherwise.
    """
    if not item_id or not source_id or not device_id:
        raise ValueError("item/source/device IDs are required")
    if start_ticks < 0:
        raise ValueError("start_ticks must be non-negative")
    audio = "ac3" if surround else "mp3"
    frame = "42" if hd else ("41" if live else "31")
    video_profile = "high" if live or hd else "baseline"
    width, height = (1920, 1080) if hd else (1280, 720)
    source_path = item_id if live else source_id
    params = [
        ("VideoCodec", "h264"), ("Profile", video_profile), ("Level", frame),
        ("MaxWidth", width), ("MaxHeight", height),
        ("VideoBitrate", quality_bps),
        ("AllowVideoStreamCopy", "false" if live else "true"),
        ("AudioCodec", audio), ("AudioBitrate", 640000 if surround else 192000),
        ("AudioSampleRate", 48000), ("MaxAudioChannels", 6 if surround else 2),
        ("MaxFramerate", 30), ("AllowAudioStreamCopy", "false"),
        ("DeviceId", device_id), ("Static", "false"),
        ("MediaSourceId", source_id), ("StartTimeTicks", start_ticks)
    ]
    if live_stream_id:
        params.append(("LiveStreamId", live_stream_id))
    if play_session_id:
        params.append(("PlaySessionId", play_session_id))
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    path = urllib.parse.quote(source_path, safe="")
    return f"{base.rstrip('/')}/Videos/{path}/stream.ts?{query}"

class JellyfinClient:
    def __init__(self, base_url: str, device_id: str | None = None):
        base_url = str(base_url or "").strip().rstrip("/")
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise JellyfinError("Jellyfin URL must use http:// or https://")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise JellyfinError("URL must not contain credentials or query parameters")
        self.base_url = base_url
        self.device_id = device_id or f"ps3-lab-{uuid.uuid4().hex[:16]}"
        self.user_id = ""
        self.token = ""
        self.timeout = 40

    def authorization(self) -> str:
        values = (f'MediaBrowser Client="{PS3_CLIENT}", Device="{PS3_DEVICE}", '
                  f'DeviceId="{self.device_id}", Version="{PS3_VERSION}"')
        if self.token:
            values += f', Token="{self.token}"'
        return values

    def headers(self) -> dict:
        return {
            "Authorization": self.authorization(),
            "User-Agent": PS3_USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }

    def request(self, path: str, method="GET", payload=None, timeout=None):
        url = self.base_url + "/" + path.lstrip("/")
        if not url.startswith(self.base_url + "/"):
            raise JellyfinError("unexpected request URL")
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = self.headers()
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as reply:
                body = reply.read(8 * 1024 * 1024)
                if not body:
                    return {}
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as ex:
            # Never print response text: plugins can include tokens/paths.
            raise JellyfinError(f"Jellyfin HTTP {ex.code} for {method} {path.split('?')[0]}") from None
        except (urllib.error.URLError, ValueError, OSError) as ex:
            raise JellyfinError(f"Jellyfin connection error ({type(ex).__name__})") from ex

    def authenticate(self, username: str, password: str) -> dict:
        if not username or not password:
            raise JellyfinError("username and password required")
        info = self.request("Users/AuthenticateByName", "POST",
                            {"Username": username, "Pw": password})
        token = info.get("AccessToken") or ""
        user = info.get("User") or {}
        if not token or not user.get("Id"):
            raise JellyfinError("server did not supply authentication token and user id")
        self.token = token
        self.user_id = user["Id"]
        return {"username": user.get("Name", ""), "device_id": self.device_id}

    def authenticate_token(self, token: str) -> dict:
        if not token:
            raise JellyfinError("access token required")
        self.token = token.strip()
        me = self.request("Users/Me")
        self.user_id = me.get("Id", "")
        if not self.user_id:
            self.token = ""
            raise JellyfinError("token did not resolve a Jellyfin user")
        return {"username": me.get("Name", ""), "device_id": self.device_id}

    def channels(self) -> list:
        if not self.user_id:
            raise JellyfinError("not authenticated")
        query = urllib.parse.urlencode({
            "UserId": self.user_id, "EnableImages": "false",
            "EnableUserData": "false"
        })
        result = self.request(f"LiveTv/Channels?{query}", timeout=60)
        return result.get("Items", []) if isinstance(result, dict) else []

    def media_items(self, limit=100) -> list:
        if not self.user_id:
            raise JellyfinError("not authenticated")
        q = urllib.parse.urlencode({
            "Recursive": "true", "IncludeItemTypes": "Movie,Episode",
            "Limit": max(1, min(250, limit)), "SortBy": "SortName"
        })
        result = self.request(f"Users/{urllib.parse.quote(self.user_id)}/Items?{q}")
        return result.get("Items", []) if isinstance(result, dict) else []

    def browse(self, category="home", start=0, limit=48,
               search="", parent_id="") -> dict:
        """Page through real Jellyfin library items, with no synthetic media.

        Series expansion uses the SeriesId filter so episodes from a complete
        series can be selected and streamed using normal PlaybackInfo.
        """
        if not self.user_id:
            raise JellyfinError("not authenticated")
        categories = {
            "home": "Movie,Episode",
            "recent": "Movie,Episode",
            "search": "Movie,Series,Episode",
            "movies": "Movie",
            "shows": "Series",
            "episodes": "Episode",
            "resume": "Movie,Episode",
        }
        if category not in categories and category != "live":
            raise JellyfinError("unsupported library category")
        start = max(0, int(start))
        limit = max(1, min(100, int(limit)))
        search = str(search or "").strip()[:120]
        if category == "live":
            params = {
                "UserId": self.user_id, "StartIndex": start,
                "Limit": limit, "EnableImages": "true",
                "EnableUserData": "true",
            }
            if search:
                params["SearchTerm"] = search
            data = self.request("LiveTv/Channels?" + urllib.parse.urlencode(params))
        else:
            params = {
                "StartIndex": start, "Limit": limit,
                "Recursive": "true", "IncludeItemTypes": categories[category],
                "Fields": "Overview,PrimaryImageAspectRatio,DateCreated,MediaSources",
                "EnableImageTypes": "Primary,Backdrop,Thumb",
                "EnableUserData": "true",
            }
            if category in ("home", "recent"):
                params.update({"SortBy": "DateCreated", "SortOrder": "Descending"})
            else:
                params.update({"SortBy": "SortName", "SortOrder": "Ascending"})
            if search:
                params["SearchTerm"] = search
            if parent_id:
                # The SeriesId filter is stable for episodes even when the
                # actual parent is a season rather than the series itself.
                params["SeriesId"] = parent_id
                params["IncludeItemTypes"] = "Episode"
            prefix = ("Users/" + urllib.parse.quote(self.user_id, safe="") +
                      ("/Items/Resume" if category == "resume" else "/Items"))
            if category == "resume":
                params.pop("SortBy", None)
                params.pop("SortOrder", None)
                params.pop("Recursive", None)
                params.pop("IncludeItemTypes", None)
            data = self.request(prefix + "?" + urllib.parse.urlencode(params))
        if not isinstance(data, dict):
            raise JellyfinError("Jellyfin returned an invalid item page")
        items = data.get("Items")
        if not isinstance(items, list):
            raise JellyfinError("Jellyfin item list missing")
        return {
            "Items": [item for item in items if isinstance(item, dict)],
            "TotalRecordCount": data.get("TotalRecordCount", len(items)),
            "StartIndex": start,
        }

    def series_episodes(self, series_id: str) -> dict:
        """List real seasons/episodes using Jellyfin's dedicated Shows endpoint."""
        if not self.user_id or not series_id:
            raise JellyfinError("authentication and series id are required")
        series = urllib.parse.quote(str(series_id), safe="")
        params = urllib.parse.urlencode({
            "UserId": self.user_id,
            "Fields": "Overview,PrimaryImageAspectRatio",
            "EnableUserData": "true",
            "Limit": 250,
        })
        payload = self.request(f"Shows/{series}/Episodes?{params}", timeout=45)
        if not isinstance(payload, dict) or not isinstance(payload.get("Items"), list):
            raise JellyfinError("Jellyfin returned an invalid episode list")
        return payload

    def image_bytes(self, item: dict, image_type="Primary") -> bytes:
        """Authenticated, size-bounded poster/backdrop fetch without URL tokens."""
        if not self.user_id or not isinstance(item, dict):
            return b""
        if image_type not in ("Primary", "Backdrop", "Thumb"):
            raise JellyfinError("unsupported image kind")
        tags = item.get("ImageTags") or {}
        if not isinstance(tags, dict):
            tags = {}
        image_id = item.get("PrimaryImageItemId") if image_type == "Primary" else None
        if not image_id:
            image_id = item.get("Id")
        if not isinstance(image_id, str) or not image_id:
            return b""
        # Some Jellyfin libraries have series artwork only; fall back through
        # server-provided image IDs rather than constructing untrusted URLs.
        if image_type == "Backdrop":
            has_art = (tags.get("Backdrop") or
                       item.get("BackdropImageTags") or
                       item.get("ParentBackdropImageTags"))
        else:
            has_art = tags.get(image_type) or image_id != item.get("Id") or (
                image_type == "Primary" and item.get("SeriesPrimaryImageTag"))
        if not has_art:
            return b""
        max_width = 1250 if image_type == "Backdrop" else 300
        route = ("Items/" + urllib.parse.quote(image_id, safe="") +
                 "/Images/" + image_type + "?maxWidth=" + str(max_width) +
                 "&quality=78")
        req = urllib.request.Request(self.base_url + "/" + route,
                                     headers=self.headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                mime = response.headers.get("Content-Type", "").lower()
                if not mime.startswith("image/"):
                    return b""
                body = response.read(4 * 1024 * 1024 + 1)
                return body if len(body) <= 4 * 1024 * 1024 else b""
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
            return b""

    def playback_info(self, item_id: str, hd=False, surround=False,
                      source_id="", live=False) -> dict:
        if not self.user_id:
            raise JellyfinError("not authenticated")
        params = [
            ("UserId", self.user_id),
            ("MaxStreamingBitrate", 10000000 if hd else 8000000),
            ("StartTimeTicks", "0"),
            ("AutoOpenLiveStream", "true" if live else "false"),
        ]
        if source_id:
            params.append(("MediaSourceId", source_id))
        url = "Items/" + urllib.parse.quote(item_id, safe="") + "/PlaybackInfo?"
        url += urllib.parse.urlencode(params)
        return self.request(url, "POST", {"DeviceProfile": ps3_device_profile(hd, surround)}, timeout=90)

    def make_session(self, item: dict, hd=False, surround=False,
                     quality_bps=4000000) -> PlaybackSession:
        item_id = item.get("Id") or ""
        if not item_id:
            raise JellyfinError("selected item has no Id")
        live = item.get("Type") == "TvChannel"
        response = self.playback_info(item_id, hd=hd, surround=surround, live=live)
        sources = response.get("MediaSources") or []
        if not sources:
            raise JellyfinError("PlaybackInfo returned no MediaSources")
        selected = sources[0]
        sid = selected.get("Id") or item_id
        playid = response.get("PlaySessionId") or ""
        liveid = selected.get("LiveStreamId") or ""
        link = build_stream_url(
            self.base_url, item_id, sid, self.device_id, playid, liveid, live,
            hd, surround, quality_bps=quality_bps)
        return PlaybackSession(
            item_id=item_id, source_id=sid, play_session_id=playid,
            live_stream_id=liveid, live=live, title=item.get("Name") or "Untitled", url=link)

    def playstate(self, session: PlaybackSession, action: str,
                  pos_ticks=0, paused=False):
        endpoints = {"start": "/Sessions/Playing",
                     "progress": "/Sessions/Playing/Progress",
                     "stopped": "/Sessions/Playing/Stopped"}
        if action not in endpoints:
            raise ValueError("invalid playstate")
        body = {
            "ItemId": session.item_id,
            "PlaySessionId": session.play_session_id,
            "PositionTicks": 0 if session.live else max(0, int(pos_ticks)),
            "IsPaused": bool(paused) if action == "progress" else False,
            "PlayMethod": "Transcode", "CanSeek": not session.live
        }
        # PS3 source deliberately omits LiveStreamId at start/progress:
        # Jellyfin 10.11 may crash on premature live-session registration.
        if action == "stopped" and session.live_stream_id:
            body["LiveStreamId"] = session.live_stream_id
        return self.request(endpoints[action], "POST", body)

    def surf_status(self, catalog_base: str, mode: str):
        if mode not in ("all", "sports", "movies-series"):
            raise ValueError("invalid Surf mode")
        parsed = urllib.parse.urlsplit(catalog_base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise JellyfinError("CatalogService URL invalid")
        url = catalog_base.rstrip("/") + "/api/surf/" + mode
        req = urllib.request.Request(url, headers=self.headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.load(response)
        except (OSError, ValueError) as ex:
            raise JellyfinError(f"Surf status request failed ({type(ex).__name__})") from ex
