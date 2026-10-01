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


def _key(name: str, group: str) -> str:
    return fold(_PREFIX.sub("", name)) + " " + fold(name) + " " + fold(group)


def _year(value: object) -> str:
    m = re.search(r"(19|20)\d{2}", str(value or ""))
    return m.group(0) if m else ""


def normalize_vod(streams: list[dict], categories: list[dict]) -> list[dict]:
    """get_vod_streams + get_vod_categories -> [{id, name, group, logo, ext, year, key}]."""
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
            "ext": str(s.get("container_extension") or "mp4").lower(),
            "year": _year(s.get("year") or s.get("releaseDate") or name),
            "key": _key(name, group),
        })
    return out


def normalize_series(series: list[dict], categories: list[dict]) -> list[dict]:
    """get_series + get_series_categories -> [{id, name, group, logo, year, key}]."""
    names = {str(c.get("category_id")): str(c.get("category_name") or "").strip() for c in categories or [] if isinstance(c, dict)}
    out = []
    for s in series or []:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("series_id") or "")
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
            "logo": str(s.get("cover") or ""),
            "year": _year(s.get("year") or s.get("releaseDate") or s.get("release_date") or name),
            "key": _key(name, group),
        })
    return out


def _subtitles(*sources: object) -> list[dict]:
    """External subtitle files some panels list in their info: [{lang, url}]."""
    out = []
    for src in sources:
        if not isinstance(src, list):
            continue
        for s in src:
            if isinstance(s, str):
                s = {"url": s}
            if not isinstance(s, dict):
                continue
            url = str(s.get("url") or s.get("file") or s.get("src") or "")
            if not url.startswith(("http://", "https://")):
                continue
            lang = str(s.get("lang") or s.get("language") or s.get("label") or s.get("name") or "").strip()
            out.append({"lang": lang or f"#{len(out) + 1}", "url": url})
    return out


def _minutes(info: dict) -> int:
    try:
        secs = int(info.get("duration_secs") or 0)
    except (TypeError, ValueError):
        secs = 0
    if not secs:
        m = re.match(r"^(\d+):(\d{2}):(\d{2})", str(info.get("duration") or ""))
        if m:
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return round(secs / 60)


def normalize_vod_info(data: dict, fallback_ext: str = "mp4") -> dict:
    """get_vod_info -> {name, plot, year, minutes, genre, cover, ext, subtitles}."""
    info = data.get("info") if isinstance(data, dict) and isinstance(data.get("info"), dict) else {}
    movie = data.get("movie_data") if isinstance(data, dict) and isinstance(data.get("movie_data"), dict) else {}
    return {
        "name": str(movie.get("name") or info.get("name") or "").strip(),
        "plot": str(info.get("plot") or info.get("description") or "").strip(),
        "year": _year(info.get("releasedate") or info.get("release_date") or info.get("year")),
        "minutes": _minutes(info),
        "genre": str(info.get("genre") or "").strip(),
        "cover": str(info.get("movie_image") or info.get("cover_big") or ""),
        "ext": str(movie.get("container_extension") or fallback_ext).lower(),
        "subtitles": _subtitles(info.get("subtitles"), movie.get("subtitles")),
    }


def normalize_series_info(data: dict) -> dict:
    """get_series_info -> {name, plot, year, cover, seasons: [{season, episodes: [...]}]}."""
    info = data.get("info") if isinstance(data, dict) and isinstance(data.get("info"), dict) else {}
    raw = data.get("episodes") if isinstance(data, dict) else None
    if isinstance(raw, list):  # some panels send a list of seasons instead of a dict
        raw = {str(i + 1): eps for i, eps in enumerate(raw)}
    seasons = []
    for num, eps in (raw or {}).items():
        if not isinstance(eps, list):
            continue
        episodes = []
        for e in eps:
            if not isinstance(e, dict) or not str(e.get("id") or "").isdigit():
                continue
            ei = e.get("info") if isinstance(e.get("info"), dict) else {}
            try:
                ep = int(e.get("episode_num") or 0)
            except (TypeError, ValueError):
                ep = 0
            episodes.append({
                "id": str(e["id"]),
                "ep": ep,
                "title": str(e.get("title") or "").strip(),
                "plot": str(ei.get("plot") or "").strip(),
                "minutes": _minutes(ei),
                "ext": str(e.get("container_extension") or "mp4").lower(),
                "subtitles": _subtitles(ei.get("subtitles"), e.get("subtitles")),
            })
        episodes.sort(key=lambda x: x["ep"])
        try:
            season = int(num)
        except (TypeError, ValueError):
            season = 0
        if episodes:
            seasons.append({"season": season, "episodes": episodes})
    seasons.sort(key=lambda s: s["season"])
    return {
        "name": str(info.get("name") or "").strip(),
        "plot": str(info.get("plot") or "").strip(),
        "year": _year(info.get("releaseDate") or info.get("release_date") or info.get("year")),
        "cover": str(info.get("cover") or ""),
        "seasons": seasons,
    }


_SRT_TIME = re.compile(r"(\d{1,2}:\d{2}:\d{2}),(\d{3})")


def srt_to_vtt(text: str) -> str:
    """Convert SubRip to WebVTT (browsers only accept WebVTT in <track>)."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if text.lstrip().startswith("WEBVTT"):
        return text
    return "WEBVTT\n\n" + _SRT_TIME.sub(r"\1.\2", text)
