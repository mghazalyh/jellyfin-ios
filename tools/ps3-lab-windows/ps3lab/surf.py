"""CatalogService Surf control contract and per-playback selection state.

Reference: JellyFin-PS3 v3.6.5-surf.1 source/player/core/player_menu.cpp and
source/player/threads/player_threads.cpp.  Selecting a previous channel is a
client-side selection for Favorites only; it never switches playback.  A
selection carries the exact 32-character token returned by CatalogService and
that immutable token is sent back with Favorite Selected Previous.
"""
from __future__ import annotations

from dataclasses import dataclass
import urllib.parse

SURF_ITEMS = {
    "Surf: All Channels": "all",
    "Surf: Sports": "sports",
    "Surf: Movies & Series": "movies-series",
}
SURF_MODES = frozenset(SURF_ITEMS.values())
SURF_ACTIONS = frozenset({
    "pause", "resume", "favorite", "previous", "favorite_previous",
    "current", "status",
})
SURF_ENDPOINTS = frozenset({"pause", "resume", "favorite", "telemetry"})
TOKEN_LENGTH = 32


class SurfError(RuntimeError):
    pass


def mode_for_item(item: dict | None) -> str:
    """Return the CatalogService mode for a Surf virtual channel, else ""."""
    if not isinstance(item, dict):
        return ""
    return SURF_ITEMS.get(item.get("Name") or "", "")


def validate_mode(mode: str) -> str:
    if mode not in SURF_MODES:
        raise SurfError("invalid Surf mode")
    return mode


def token_valid(token) -> bool:
    if not isinstance(token, str) or len(token) != TOKEN_LENGTH:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in token)


def validate_catalog_url(catalog_base: str) -> str:
    """Accept a CatalogService root or its /api/surf base without credentials."""
    raw = str(catalog_base or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SurfError("CatalogService URL must use http:// or https://")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SurfError("CatalogService URL must not contain credentials or query parameters")
    path = parsed.path.rstrip("/")
    if path not in ("", "/api/surf"):
        raise SurfError("CatalogService URL must be a host or its /api/surf base")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def surf_url(catalog_base: str, mode: str, action: str | None = None) -> str:
    validate_mode(mode)
    if action is not None and action not in SURF_ENDPOINTS:
        raise SurfError("invalid Surf endpoint")
    base = validate_catalog_url(catalog_base)
    path = base + "/api/surf/" + mode
    if action:
        path += "/" + action
    return path


@dataclass(frozen=True)
class SurfSelection:
    source: str
    token: str

    def __post_init__(self):
        if not isinstance(self.source, str) or not self.source:
            raise SurfError("Surf selection source is required")
        if not token_valid(self.token):
            raise SurfError("Surf selection token must be 32 hexadecimal characters")


@dataclass(frozen=True)
class SurfStatus:
    source: str = ""
    state: str = ""
    group: str = ""
    source_id: str = ""
    boot: str = ""
    recent_sources: tuple[SurfSelection, ...] = ()


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def parse_status(payload) -> SurfStatus:
    if not isinstance(payload, dict):
        raise SurfError("Surf status response is invalid")
    recent = []
    entries = payload.get("recent_sources")
    if isinstance(entries, list):
        for entry in entries:
            if len(recent) >= 16:
                break
            if not isinstance(entry, dict):
                continue
            source = entry.get("source")
            token = entry.get("token")
            if isinstance(source, str) and source and token_valid(token):
                recent.append(SurfSelection(source, token))
    return SurfStatus(
        source=_text(payload.get("source")),
        state=_text(payload.get("state")),
        group=_text(payload.get("group")),
        source_id=_text(payload.get("source_id")),
        boot=_text(payload.get("boot")),
        recent_sources=tuple(recent),
    )


@dataclass(frozen=True)
class SurfResult:
    action: str
    mode: str
    generation: int
    request_id: int
    ok: bool
    message: str
    http_status: int = 0
    status: SurfStatus | None = None
    selection: SurfSelection | None = None


class SurfSession:
    """Display-thread-owned selection state tied to one playback generation."""

    def __init__(self):
        self._generation = 0
        self._mode = ""
        self._selection: SurfSelection | None = None

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def selection(self) -> SurfSelection | None:
        return self._selection

    def begin(self, mode: str) -> int:
        validate_mode(mode)
        self._generation += 1
        self._mode = mode
        self._selection = None
        return self._generation

    def reset(self) -> int:
        self._generation += 1
        self._mode = ""
        self._selection = None
        return self._generation

    def is_current(self, generation: int, mode: str = "") -> bool:
        return (generation == self._generation and
                (not mode or mode == self._mode))

    def select_current(self, generation: int | None = None,
                       mode: str = "") -> bool:
        if generation is not None and not self.is_current(generation, mode):
            return False
        self._selection = None
        return True

    def choose_previous(self, generation: int, mode: str,
                        status: SurfStatus) -> SurfSelection | None:
        """Return the next recent source without changing playback."""
        if not self.is_current(generation, mode) or not status.recent_sources:
            return None
        entries = status.recent_sources
        next_index = 0
        if self._selection is not None:
            for index, entry in enumerate(entries):
                if entry.token == self._selection.token:
                    next_index = (index + 1) % len(entries)
                    break
        return entries[next_index]

    def apply_selection(self, generation: int, mode: str,
                        selection: SurfSelection | None) -> bool:
        if not self.is_current(generation, mode):
            return False
        self._selection = selection
        return True
