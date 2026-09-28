"""Pure helpers for the Xtream short EPG (no Home Assistant imports, unit-testable)."""

from __future__ import annotations

import base64
import binascii


def _b64(value: str | None) -> str:
    if not value:
        return ""
    try:
        return base64.b64decode(value, validate=False).decode("utf-8", "replace").strip()
    except (binascii.Error, ValueError):
        return str(value).strip()


def normalize_listings(listings: list[dict], now: float, limit: int = 4) -> list[dict]:
    """Decode, drop ended and duplicate/overlapping entries, return the next `limit` programmes.

    Providers often merge two guide sources, giving near-identical entries that overlap
    ("RENZE" 14:16-15:14 and "Renze" 14:25-15:20); keep the first of any pair that
    overlaps the previous kept entry by more than half of its own duration.
    """
    items = []
    for it in listings or []:
        try:
            start = int(it.get("start_timestamp") or 0)
            end = int(it.get("stop_timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if not start or end <= start or end <= now:
            continue
        title = _b64(it.get("title"))
        if not title:
            continue
        items.append({"title": title, "desc": _b64(it.get("description")), "start": start, "end": end})
    items.sort(key=lambda x: (x["start"], -x["end"]))

    kept: list[dict] = []
    for it in items:
        if kept:
            last = kept[-1]
            overlap = min(last["end"], it["end"]) - max(last["start"], it["start"])
            if overlap > 0 and (
                it["title"].casefold() == last["title"].casefold()
                or overlap > (it["end"] - it["start"]) / 2
            ):
                continue
        kept.append(it)
        if len(kept) >= limit:
            break
    return kept
