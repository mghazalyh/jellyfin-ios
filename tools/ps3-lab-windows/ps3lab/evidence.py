"""Bounded, redacted, JSON-safe structured evidence records.

Evidence values are either local measurements (for example transport PTS/PCR
observations) or explicitly ``simulated_*`` model results. Untrusted or
missing values must never be promoted into a physical measurement by export.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math

SENSITIVE_KEYS = frozenset((
    "token", "access_token", "authorization", "password", "secret",
    "api_key", "cookie", "set-cookie", "url", "path", "item_id",
    "source_id", "title", "username", "headers",
))
MAX_TEXT = 92
MAX_ITEMS = 64
MAX_DEPTH = 6


def _sanitize_value(value, depth=0):
    if depth > MAX_DEPTH:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if "://" in value or "token=" in value.lower():
            return "[redacted]"
        return value[:MAX_TEXT]
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, depth + 1) for item in value[:MAX_ITEMS]]
    if isinstance(value, dict):
        clean = {}
        for key, item in list(value.items())[:MAX_ITEMS]:
            name = str(key)
            if name.lower() in SENSITIVE_KEYS:
                continue
            clean[name] = _sanitize_value(item, depth + 1)
        return clean
    return None


def sanitize_evidence(event, data=None, now=None) -> dict:
    """Return one durable evidence record with no credentials or raw URLs."""
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    return {
        "utc": timestamp,
        "event": str(event)[:MAX_TEXT],
        "data": _sanitize_value(data or {}),
    }
