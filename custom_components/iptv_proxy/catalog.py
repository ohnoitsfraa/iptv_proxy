"""Pure helpers for the provider's channel list and guide search (no Home Assistant imports, unit-testable)."""

from __future__ import annotations

import re
import unicodedata

# categories left out of search results (the pop-up is usually a family screen)
_EXCLUDED_CATEGORY = re.compile(r"xxx|adult|\b18\s*\+", re.IGNORECASE)
# provider prefixes such as "DE|", "UK:", "|NL|" add noise to names
_PREFIX = re.compile(r"^\s*\|?\s*[A-Z]{2,4}\s*[|:]\s*")


def fold(text: str | None) -> str:
    """Lower-case, strip accents and collapse punctuation to single spaces."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return " ".join(re.sub(r"[^\w+]+", " ", text).split())


def normalize_streams(streams: list[dict], categories: list[dict]) -> list[dict]:
    """Turn get_live_streams + get_live_categories into [{id, name, group, logo, key}]."""
    names = {str(c.get("category_id")): str(c.get("category_name") or "").strip() for c in categories or [] if isinstance(c, dict)}
    out = []
    for s in streams or []:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("stream_id") or "")
        name = str(s.get("name") or "").strip()
        if not sid.isdigit() or not name:
            continue
        group = names.get(str(s.get("category_id")), "")
        if _EXCLUDED_CATEGORY.search(group) or _EXCLUDED_CATEGORY.search(name):
            continue
        out.append({
            "id": sid,
            "name": name,
            "group": group,
            "logo": str(s.get("stream_icon") or ""),
            "key": fold(_PREFIX.sub("", name)) + " " + fold(name) + " " + fold(group),
        })
    return out


def search_streams(catalog: list[dict], query: str, limit: int = 30) -> list[dict]:
    """Channels whose name/group contains every word of `query`; word-start matches and short names first."""
    words = fold(query).split()
    if not words:
        return []
    hits = []
    for ch in catalog:
        key = ch["key"]
        if all(w in key for w in words):
            starts = sum(1 for w in words if re.search(r"(^|\s)" + re.escape(w), key))
            hits.append((-starts, len(ch["name"]), ch["name"], ch))
    hits.sort(key=lambda h: h[:3])
    return [{k: v for k, v in h[3].items() if k != "key"} for h in hits[:limit]]


def search_programmes(guide: dict[str, list[dict]], query: str, now: float, limit: int = 40) -> list[dict]:
    """Programmes (already normalised by epg.normalize_listings) whose title, or else description, matches.

    Returns [{id, title, desc, start, end, live}], title matches before description matches,
    then what's on now, then by start time.
    """
    words = fold(query).split()
    if not words:
        return []
    hits = []
    for sid, items in guide.items():
        for p in items:
            title = fold(p["title"])
            in_title = all(w in title for w in words)
            if not in_title and not all(w in fold(p["title"] + " " + p.get("desc", "")) for w in words):
                continue
            live = p["start"] <= now < p["end"]
            hits.append((not in_title, not live, p["start"], {**p, "id": sid, "live": live}))
    hits.sort(key=lambda h: h[:3])
    return [h[3] for h in hits[:limit]]
