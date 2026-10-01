"""IPTV proxy: serves an Xtream Codes provider's HLS streams and logos through Home Assistant.

Only bytes are passed through (no transcoding). All endpoints require Home Assistant auth;
the dashboard uses signed paths, and playlists are rewritten to signed proxy paths.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
import shutil
import time
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientTimeout, web
import voluptuous as vol

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .catalog import (
    normalize_series,
    normalize_series_info,
    normalize_streams,
    normalize_vod,
    normalize_vod_info,
    search_programmes,
    search_streams,
    srt_to_vtt,
)
from .const import BASE, CONF_HOST, CONF_PASSWORD, CONF_USERNAME, DOMAIN, USER_AGENT
from .epg import normalize_listings
from .playlist import base_domain, host_allowed, rewrite_playlist

_LOGGER = logging.getLogger(__name__)

KEY_REFRESH_TOKEN_ID = "hass_refresh_token_id"
SEGMENT_SIGN_TTL = timedelta(minutes=15)
HEADERS = {"User-Agent": USER_AGENT}
EPG_TTL = {False: 300, True: 900}  # short (now/next) vs full-day guide, seconds
EPG_MAX_IDS = 80
SEARCH_MAX_IDS = 120
CATALOG_TTL = 6 * 3600  # provider channel list, seconds
VOD_INFO_TTL = 3600
PROBE_TTL = 24 * 3600
# what browsers can decode; anything else is remuxed with the audio converted to AAC
BROWSER_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}
LIBRARY_RETRY = 600  # after a failed film/series list download, seconds before trying again
VOD_EXTENSIONS = {"mp4", "m4v", "mkv", "avi", "mov", "webm", "ts", "flv", "wmv", "mpg", "mpeg"}
VOD_MIME = {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
            "mkv": "video/x-matroska", "ts": "video/mp2t", "avi": "video/x-msvideo"}
VOD_PATH = {"movie": "movie", "episode": "series"}  # card kind -> Xtream path segment
SERVICE_FIND_CHANNELS = "find_channels"
SERVICE_INSPECT_VOD = "inspect_vod"
SERVICE_FIND_VOD = "find_vod"
FIND_VOD_SCHEMA = vol.Schema({
    vol.Required("query"): cv.string,
    vol.Optional("limit", default=20): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
})
INSPECT_VOD_SCHEMA = vol.Schema({
    vol.Required("kind"): vol.In(["movie", "episode"]),
    vol.Required("id"): vol.All(cv.string, vol.Match(r"^\d+$")),
    vol.Optional("extension"): vol.All(cv.string, vol.In(sorted(VOD_EXTENSIONS))),
})
FIND_CHANNELS_SCHEMA = vol.Schema({
    vol.Required("query"): cv.string,
    vol.Optional("limit", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
})


class _State:
    """Runtime state shared by the views."""

    def __init__(self, entry: ConfigEntry) -> None:
        self.host = entry.data[CONF_HOST].rstrip("/")
        self.username = entry.data[CONF_USERNAME]
        self.password = entry.data[CONF_PASSWORD]
        self.domain = base_domain(urlsplit(self.host).hostname or "")
        self.allowed_hosts: set[str] = {(urlsplit(self.host).hostname or "").lower()}
        self.epg_cache: dict[tuple[str, bool], tuple[float, list]] = {}
        self.epg_sem = asyncio.Semaphore(6)
        self.catalog: tuple[float, list[dict]] | None = None
        self.catalog_lock = asyncio.Lock()
        self.library: dict[str, tuple[float, list[dict]]] = {}  # "movies" / "series"
        self.library_failed: dict[str, float] = {}
        self.library_task: asyncio.Task | None = None
        self.vod_info: dict[tuple[str, str], tuple[float, dict]] = {}
        self.probes: dict[tuple[str, str, str], tuple[float, dict]] = {}
        self.remux_proc: asyncio.subprocess.Process | None = None

    def vod_url(self, kind: str, stream_id: str, ext: str) -> str:
        return f"{self.host}/{VOD_PATH[kind]}/{self.username}/{self.password}/{stream_id}.{ext}"


async def _player_api(hass: HomeAssistant, state: _State, params: dict, timeout: float = 20) -> object | None:
    """GET player_api.php with the account's login; None when the provider fails."""
    params = {"username": state.username, "password": state.password, **params}
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{state.host}/player_api.php", params=params, headers=HEADERS, timeout=ClientTimeout(total=timeout)
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except (ClientError, TimeoutError, ValueError):
        return None


async def _player_api_large(hass: HomeAssistant, state: _State, params: dict) -> object | None:
    """Like _player_api, for lists that can be many megabytes: JSON is parsed off the event loop."""
    params = {"username": state.username, "password": state.password, **params}
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{state.host}/player_api.php", params=params, headers=HEADERS, timeout=ClientTimeout(total=180)
        ) as resp:
            if resp.status != 200:
                return None
            raw = await resp.read()
    except (ClientError, TimeoutError):
        return None
    try:
        return await hass.async_add_executor_job(json.loads, raw)
    except ValueError:
        return None


async def _load_library_kind(hass: HomeAssistant, state: _State, kind: str) -> None:
    """Download and normalise one list ("movies" or "series"); keep the old one if the provider fails."""
    if kind == "movies":
        items, cats = await asyncio.gather(
            _player_api_large(hass, state, {"action": "get_vod_streams"}),
            _player_api(hass, state, {"action": "get_vod_categories"}),
        )
        normalize = normalize_vod
    else:
        items, cats = await asyncio.gather(
            _player_api_large(hass, state, {"action": "get_series"}),
            _player_api(hass, state, {"action": "get_series_categories"}),
        )
        normalize = normalize_series
    if not isinstance(items, list):
        state.library_failed[kind] = time.time()
        return
    # tens of thousands of titles: normalising them must not block the event loop
    result = await hass.async_add_executor_job(normalize, items, cats if isinstance(cats, list) else [])
    state.library[kind] = (time.time(), result)
    state.library_failed.pop(kind, None)


def _library_due(state: _State, kind: str, now: float) -> bool:
    if now - state.library_failed.get(kind, 0) < LIBRARY_RETRY:
        return False
    cached = state.library.get(kind)
    return cached is None or now - cached[0] > CATALOG_TTL


async def async_library(hass: HomeAssistant, state: _State, wait: bool = True) -> tuple[list[dict], list[dict]]:
    """The provider's films and series.

    Loaded on first use and refreshed in the background after CATALOG_TTL; all callers share one
    download, and only the very first load (nothing cached yet) makes a caller wait.
    """
    now = time.time()
    due = [k for k in ("movies", "series") if _library_due(state, k, now)]
    if due and (state.library_task is None or state.library_task.done()):
        async def load() -> None:
            await asyncio.gather(*(_load_library_kind(hass, state, k) for k in due))

        state.library_task = hass.async_create_background_task(load(), f"{DOMAIN} library")
    task = state.library_task
    if wait and task is not None and not task.done() and not all(k in state.library for k in ("movies", "series")):
        await asyncio.shield(task)
    return (
        state.library.get("movies", (0, []))[1],
        state.library.get("series", (0, []))[1],
    )


async def async_vod_info(hass: HomeAssistant, state: _State, kind: str, item_id: str) -> dict | None:
    """Normalised get_vod_info (kind "movie") or get_series_info (kind "series"), cached for an hour."""
    key = (kind, item_id)
    cached = state.vod_info.get(key)
    if cached is not None and time.time() - cached[0] <= VOD_INFO_TTL:
        return cached[1]
    if kind == "movie":
        data = await _player_api(hass, state, {"action": "get_vod_info", "vod_id": item_id}, timeout=30)
        info = normalize_vod_info(data) if isinstance(data, dict) and data else None
    else:
        data = await _player_api(hass, state, {"action": "get_series_info", "series_id": item_id}, timeout=30)
        info = normalize_series_info(data) if isinstance(data, dict) and data else None
    if info is None:
        return cached[1] if cached else None
    subs = info.get("subtitles", []) + [s for season in info.get("seasons", []) for e in season["episodes"] for s in e["subtitles"]]
    for sub in subs:  # external subtitle files may live on another host; allow exactly those
        host = (urlsplit(sub["url"]).hostname or "").lower()
        if host:
            state.allowed_hosts.add(host)
    state.vod_info[key] = (time.time(), info)
    return info


async def async_inspect_vod(hass: HomeAssistant, state: _State, kind: str, item_id: str, ext: str | None) -> dict:
    """What a film/episode really contains: provider info, HLS availability and (with ffprobe) its tracks."""
    result: dict = {"kind": kind, "id": item_id}
    if kind == "movie":
        info = await async_vod_info(hass, state, "movie", item_id)
        result["info"] = info
        ext = ext or (info or {}).get("ext") or "mp4"
    ext = ext or "mp4"
    result["extension"] = ext
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            state.vod_url(kind, item_id, "m3u8"), headers=HEADERS, timeout=ClientTimeout(total=20)
        ) as resp:
            text = (await resp.content.read(65536)).decode("utf-8", "replace") if resp.status == 200 else ""
        result["hls"] = {
            "available": text.lstrip().startswith("#EXTM3U"),
            "subtitle_tracks": text.count("TYPE=SUBTITLES"),
            "audio_tracks": text.count("TYPE=AUDIO"),
        }
    except (ClientError, TimeoutError):
        result["hls"] = {"available": False, "error": "unreachable"}

    result["tracks"] = await async_probe(hass, state, kind, item_id, ext)
    return result


async def async_probe(hass: HomeAssistant, state: _State, kind: str, item_id: str, ext: str) -> dict:
    """Container, duration and tracks of a film/episode via ffprobe; cached. {"error": ...} on failure."""
    key = (kind, item_id, ext)
    cached = state.probes.get(key)
    if cached is not None and time.time() - cached[0] <= PROBE_TTL:
        return cached[1]
    ffprobe = await hass.async_add_executor_job(shutil.which, "ffprobe")
    if not ffprobe:
        return {"error": "ffprobe not found on this system"}
    proc = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", "-user_agent", USER_AGENT, "-show_entries",
        "stream=index,codec_type,codec_name,width,height,channels:stream_tags=language,title:format=format_name,duration",
        "-of", "json", state.vod_url(kind, item_id, ext),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
    except TimeoutError:
        proc.kill()
        return {"error": "ffprobe timed out"}
    if proc.returncode != 0:
        # stderr is discarded on purpose: it would contain the stream URL with the login
        return {"error": f"ffprobe failed (exit {proc.returncode})"}
    probe = json.loads(out or b"{}")
    fmt = probe.get("format", {})
    streams = [
        {
            "type": st.get("codec_type"),
            "codec": st.get("codec_name"),
            **({"size": f"{st['width']}x{st['height']}"} if st.get("width") else {}),
            **({"channels": st["channels"]} if st.get("channels") else {}),
            **{k: v for k, v in (st.get("tags") or {}).items() if k in ("language", "title")},
        }
        for st in probe.get("streams", [])
    ]
    audio = [st for st in streams if st["type"] == "audio"]
    video = next((st for st in streams if st["type"] == "video"), None)
    result = {
        "container": fmt.get("format_name"),
        "seconds": round(float(fmt.get("duration") or 0)),
        "minutes": round(float(fmt.get("duration") or 0) / 60),
        "video": video["codec"] if video else None,
        "audio": [{"codec": a["codec"], "channels": a.get("channels"), "language": a.get("language", ""), "title": a.get("title", "")} for a in audio],
        "subtitles": [{"codec": st["codec"], "language": st.get("language", ""), "title": st.get("title", "")} for st in streams if st["type"] == "subtitle"],
        "browser_audio": bool(audio) and audio[0]["codec"] in BROWSER_AUDIO,
        "streams": streams,
    }
    state.probes[key] = (time.time(), result)
    return result


async def async_listings(hass: HomeAssistant, state: _State, stream_id: str, full: bool) -> list:
    """Raw guide listings for one channel, cached; stale data is served if the provider fails."""
    key = (stream_id, full)
    cached = state.epg_cache.get(key)
    now = time.time()
    if cached is not None and now - cached[0] <= EPG_TTL[full]:
        return cached[1]
    params = {"stream_id": stream_id, "action": "get_simple_data_table" if full else "get_short_epg"}
    if not full:
        params["limit"] = "6"
    async with state.epg_sem:
        data = await _player_api(hass, state, params)
    if data is None:
        return cached[1] if cached else []
    listings = data.get("epg_listings") if isinstance(data, dict) else None
    listings = listings if isinstance(listings, list) else []
    state.epg_cache[key] = (now, listings)
    return listings


async def async_catalog(hass: HomeAssistant, state: _State) -> list[dict]:
    """The provider's live channels with their category names, cached for a few hours."""
    async with state.catalog_lock:
        if state.catalog is not None and time.time() - state.catalog[0] <= CATALOG_TTL:
            return state.catalog[1]
        streams, categories = await asyncio.gather(
            _player_api(hass, state, {"action": "get_live_streams"}, timeout=60),
            _player_api(hass, state, {"action": "get_live_categories"}),
        )
        if not isinstance(streams, list):
            return state.catalog[1] if state.catalog else []
        catalog = await hass.async_add_executor_job(
            normalize_streams, streams, categories if isinstance(categories, list) else []
        )
        state.catalog = (time.time(), catalog)
        return catalog


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the proxy from a config entry."""
    hass.data[DOMAIN] = _State(entry)
    if not hass.data.get(f"{DOMAIN}_views"):
        for view in (
            LiveView(hass), PlaylistView(hass), SegmentView(hass), LogoView(hass),
            EpgView(hass), StreamsView(hass), SearchView(hass),
            LibraryView(hass), VodInfoView(hass), VodPlaylistView(hass), VodProbeView(hass), VodRemuxView(hass),
            VodFileView(hass), SubtitleView(hass),
        ):
            hass.http.register_view(view)
        hass.data[f"{DOMAIN}_views"] = True

    if not hass.services.has_service(DOMAIN, SERVICE_FIND_CHANNELS):
        async def find_channels(call: ServiceCall) -> ServiceResponse:
            state = hass.data.get(DOMAIN)
            if state is None:
                raise HomeAssistantError("iptv_proxy is not configured")
            catalog = await async_catalog(hass, state)
            return {"channels": search_streams(catalog, call.data["query"], call.data["limit"])}

        hass.services.async_register(
            DOMAIN, SERVICE_FIND_CHANNELS, find_channels,
            schema=FIND_CHANNELS_SCHEMA, supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_INSPECT_VOD):
        async def inspect_vod(call: ServiceCall) -> ServiceResponse:
            state = hass.data.get(DOMAIN)
            if state is None:
                raise HomeAssistantError("iptv_proxy is not configured")
            return await async_inspect_vod(hass, state, call.data["kind"], call.data["id"], call.data.get("extension"))

        hass.services.async_register(
            DOMAIN, SERVICE_INSPECT_VOD, inspect_vod,
            schema=INSPECT_VOD_SCHEMA, supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_FIND_VOD):
        async def find_vod(call: ServiceCall) -> ServiceResponse:
            state = hass.data.get(DOMAIN)
            if state is None:
                raise HomeAssistantError("iptv_proxy is not configured")
            movies, series = await async_library(hass, state)
            q, n = call.data["query"], call.data["limit"]
            return await hass.async_add_executor_job(
                lambda: {"movies": search_streams(movies, q, n), "series": search_streams(series, q, n)}
            )

        hass.services.async_register(
            DOMAIN, SERVICE_FIND_VOD, find_vod, schema=FIND_VOD_SCHEMA, supports_response=SupportsResponse.ONLY,
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload: views stay registered but answer 503 until set up again."""
    hass.data.pop(DOMAIN, None)
    return True


class _BaseView(HomeAssistantView):
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @property
    def state(self) -> _State | None:
        return self.hass.data.get(DOMAIN)

    async def _playlist(self, request: web.Request, url: str) -> web.StreamResponse:
        state = self.state
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(url, headers=HEADERS, timeout=ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return web.Response(status=502, text=f"upstream status {resp.status}")
                text = await resp.text(errors="replace")
                final_url = str(resp.url)
        except (ClientError, TimeoutError):
            return web.Response(status=504, text="upstream unreachable")
        if not text.lstrip().startswith("#EXTM3U"):
            return web.Response(status=502, text="upstream did not return a playlist")

        final_host = (urlsplit(final_url).hostname or "").lower()
        if final_host:
            state.allowed_hosts.add(final_host)
        token_id = request.get(KEY_REFRESH_TOKEN_ID)

        def sign(path: str) -> str:
            return async_sign_path(self.hass, path, SEGMENT_SIGN_TTL, refresh_token_id=token_id)

        body, hosts = rewrite_playlist(text, final_url, BASE, sign)
        state.allowed_hosts.update(hosts)
        return web.Response(
            text=body,
            content_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )

    async def _passthrough(self, request: web.Request, url: str, cache: str) -> web.StreamResponse:
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(url, headers=HEADERS, timeout=ClientTimeout(total=None, sock_connect=10, sock_read=30)) as up:
                if up.status != 200:
                    return web.Response(status=502, text=f"upstream status {up.status}")
                resp = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": up.headers.get("Content-Type", "application/octet-stream"),
                        "Cache-Control": cache,
                    },
                )
                if up.content_length is not None:
                    resp.content_length = up.content_length
                await resp.prepare(request)
                try:
                    async for chunk in up.content.iter_chunked(64 * 1024):
                        await resp.write(chunk)
                except (ConnectionResetError, ClientError):
                    pass  # viewer went away or upstream hiccup; nothing to clean up
                return resp
        except (ClientError, TimeoutError):
            return web.Response(status=504, text="upstream unreachable")


    async def _file(self, request: web.Request, url: str, ext: str) -> web.StreamResponse:
        """Stream a film/episode file, forwarding Range so the player can seek."""
        headers = dict(HEADERS)
        if rng := request.headers.get("Range"):
            headers["Range"] = rng
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(url, headers=headers, timeout=ClientTimeout(total=None, sock_connect=15, sock_read=60)) as up:
                if up.status == 416:
                    return web.Response(status=416, headers={"Content-Range": up.headers.get("Content-Range", "")})
                if up.status not in (200, 206):
                    return web.Response(status=502, text=f"upstream status {up.status}")
                ctype = up.headers.get("Content-Type", "")
                out = {
                    "Content-Type": ctype if ctype.startswith("video/") else VOD_MIME.get(ext, "application/octet-stream"),
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-cache",
                }
                if up.status == 206 and up.headers.get("Content-Range"):
                    out["Content-Range"] = up.headers["Content-Range"]
                resp = web.StreamResponse(status=up.status, headers=out)
                if up.content_length is not None:
                    resp.content_length = up.content_length
                await resp.prepare(request)
                try:
                    async for chunk in up.content.iter_chunked(256 * 1024):
                        await resp.write(chunk)
                except (ConnectionResetError, ClientError):
                    pass  # the player seeks by dropping the request; that's normal
                return resp
        except (ClientError, TimeoutError):
            return web.Response(status=504, text="upstream unreachable")


class LiveView(_BaseView):
    """Entry playlist for a live channel by Xtream stream id."""

    url = BASE + "/live/{stream_id}.m3u8"
    name = "api:iptv_proxy:live"

    async def get(self, request: web.Request, stream_id: str) -> web.StreamResponse:
        state = self.state
        if state is None:
            return web.Response(status=503, text="iptv_proxy not configured")
        if not stream_id.isdigit():
            return web.Response(status=404)
        url = f"{state.host}/live/{state.username}/{state.password}/{stream_id}.m3u8"
        return await self._playlist(request, url)


class PlaylistView(_BaseView):
    """Nested/variant playlists referenced by an upstream playlist."""

    url = BASE + "/pl"
    name = "api:iptv_proxy:pl"

    async def get(self, request: web.Request) -> web.StreamResponse:
        state = self.state
        url = request.query.get("u", "")
        if state is None or not host_allowed(url, state.allowed_hosts, state.domain):
            return web.Response(status=403)
        return await self._playlist(request, url)


class SegmentView(_BaseView):
    """Media segments (MPEG-TS / fMP4), streamed through unchanged."""

    url = BASE + "/seg"
    name = "api:iptv_proxy:seg"

    async def get(self, request: web.Request) -> web.StreamResponse:
        state = self.state
        url = request.query.get("u", "")
        if state is None or not host_allowed(url, state.allowed_hosts, state.domain):
            return web.Response(status=403)
        return await self._passthrough(request, url, "no-cache")


class LogoView(_BaseView):
    """Channel logos from the provider's own domain (e.g. its picon server)."""

    url = BASE + "/logo"
    name = "api:iptv_proxy:logo"

    async def get(self, request: web.Request) -> web.StreamResponse:
        state = self.state
        url = request.query.get("u", "")
        if state is None or not host_allowed(url, set(), state.domain):
            return web.Response(status=403)
        return await self._passthrough(request, url, "private, max-age=86400")


class EpgView(_BaseView):
    """Programme guide: GET ?ids=1,2,3 -> {id: [{title, desc, start, end}, ...]}; add full=1 for a longer list."""

    url = BASE + "/epg"
    name = "api:iptv_proxy:epg"

    async def get(self, request: web.Request) -> web.StreamResponse:
        state = self.state
        if state is None:
            return web.Response(status=503, text="iptv_proxy not configured")
        ids = [i for i in request.query.get("ids", "").split(",") if i.isdigit()][:EPG_MAX_IDS]
        full = request.query.get("full") == "1"

        async def one(stream_id: str) -> tuple[str, list]:
            listings = await async_listings(self.hass, state, stream_id, full)
            return stream_id, normalize_listings(listings, time.time(), limit=8 if full else 2)

        results = await asyncio.gather(*(one(i) for i in ids))
        return self.json(dict(results), headers={"Cache-Control": "private, max-age=60"})


class StreamsView(_BaseView):
    """Search the provider's channel list: GET ?q=ard&limit=30 -> [{id, name, group, logo}]."""

    url = BASE + "/streams"
    name = "api:iptv_proxy:streams"

    async def get(self, request: web.Request) -> web.StreamResponse:
        state = self.state
        if state is None:
            return web.Response(status=503, text="iptv_proxy not configured")
        try:
            limit = max(1, min(100, int(request.query.get("limit", "30"))))
        except ValueError:
            limit = 30
        catalog = await async_catalog(self.hass, state)
        return self.json(await self.hass.async_add_executor_job(search_streams, catalog, request.query.get("q", ""), limit))


class SearchView(_BaseView):
    """Search the guide of the given channels: GET ?q=journaal&ids=1,2 -> [{id, title, desc, start, end, live}].

    Without q it only fills the guide cache, so a later search answers quickly.
    """

    url = BASE + "/search"
    name = "api:iptv_proxy:search"

    async def get(self, request: web.Request) -> web.StreamResponse:
        state = self.state
        if state is None:
            return web.Response(status=503, text="iptv_proxy not configured")
        ids = list(dict.fromkeys(i for i in request.query.get("ids", "").split(",") if i.isdigit()))[:SEARCH_MAX_IDS]
        query = request.query.get("q", "")
        now = time.time()

        async def one(stream_id: str) -> tuple[str, list]:
            listings = await async_listings(self.hass, state, stream_id, True)
            return stream_id, normalize_listings(listings, now, limit=500) if query else []

        guide = dict(await asyncio.gather(*(one(i) for i in ids)))
        return self.json(search_programmes(guide, query, now), headers={"Cache-Control": "no-cache"})


class LibraryView(_BaseView):
    """Search films and series: GET ?q=matrix&limit=20 -> {movies: [...], series: [...]}; without q it only starts loading."""

    url = BASE + "/library"
    name = "api:iptv_proxy:library"

    async def get(self, request: web.Request) -> web.StreamResponse:
        state = self.state
        if state is None:
            return web.Response(status=503, text="iptv_proxy not configured")
        try:
            limit = max(1, min(50, int(request.query.get("limit", "20"))))
        except ValueError:
            limit = 20
        query = request.query.get("q", "")
        if not query.strip():  # warm-up call when the search panel opens: start loading, don't wait
            await async_library(self.hass, state, wait=False)
            return self.json({"movies": [], "series": []})
        movies, series = await async_library(self.hass, state)

        def search() -> dict:
            return {"movies": search_streams(movies, query, limit), "series": search_streams(series, query, limit)}

        return self.json(await self.hass.async_add_executor_job(search))


class VodInfoView(_BaseView):
    """Details of a film (GET /movie/{id}) or a series with its seasons and episodes (GET /series/{id})."""

    url = BASE + "/{kind:movie|series}/{item_id:\\d+}"
    name = "api:iptv_proxy:vod_info"

    async def get(self, request: web.Request, kind: str, item_id: str) -> web.StreamResponse:
        state = self.state
        if state is None:
            return web.Response(status=503, text="iptv_proxy not configured")
        info = await async_vod_info(self.hass, state, kind, item_id)
        if info is None:
            return web.Response(status=502, text="upstream did not return info")
        return self.json(info)


class VodPlaylistView(_BaseView):
    """HLS version of a film/episode, when the provider offers one: GET /vod/{movie|episode}/{id}.m3u8."""

    url = BASE + "/vod/{kind:movie|episode}/{item_id:\\d+}.m3u8"
    name = "api:iptv_proxy:vod_playlist"

    async def get(self, request: web.Request, kind: str, item_id: str) -> web.StreamResponse:
        state = self.state
        if state is None:
            return web.Response(status=503, text="iptv_proxy not configured")
        return await self._playlist(request, state.vod_url(kind, item_id, "m3u8"))


class VodProbeView(_BaseView):
    """Tracks of a film/episode, so the player can tell whether the browser can decode it: GET .../{id}.probe?ext=mkv."""

    url = BASE + "/vod/{kind:movie|episode}/{item_id:\\d+}.probe"
    name = "api:iptv_proxy:vod_probe"

    async def get(self, request: web.Request, kind: str, item_id: str) -> web.StreamResponse:
        state = self.state
        if state is None:
            return web.Response(status=503, text="iptv_proxy not configured")
        ext = request.query.get("ext", "mp4")
        if ext not in VOD_EXTENSIONS:
            return web.Response(status=404)
        return self.json(await async_probe(self.hass, state, kind, item_id, ext))


class VodRemuxView(_BaseView):
    """A film/episode remuxed to fragmented MP4 with AAC stereo audio (video copied, not transcoded).

    GET .../{id}.remux?ext=mkv&t=<start seconds>&a=<audio track index>. Not seekable by Range: the
    player seeks by requesting a new start time. Only one remux runs at a time (provider connection limits).
    """

    url = BASE + "/vod/{kind:movie|episode}/{item_id:\\d+}.remux"
    name = "api:iptv_proxy:vod_remux"

    async def get(self, request: web.Request, kind: str, item_id: str) -> web.StreamResponse:
        state = self.state
        if state is None:
            return web.Response(status=503, text="iptv_proxy not configured")
        ext = request.query.get("ext", "mkv")
        if ext not in VOD_EXTENSIONS:
            return web.Response(status=404)
        try:
            start = max(0.0, float(request.query.get("t", "0")))
            audio = max(0, int(request.query.get("a", "0")))
        except ValueError:
            return web.Response(status=400)
        ffmpeg = await self.hass.async_add_executor_job(shutil.which, "ffmpeg")
        if not ffmpeg:
            return web.Response(status=501, text="ffmpeg not found on this system")
        probe = await async_probe(self.hass, state, kind, item_id, ext)
        tag = ["-tag:v", "hvc1"] if probe.get("video") == "hevc" else []  # Safari needs hvc1 for HEVC in MP4

        if state.remux_proc and state.remux_proc.returncode is None:
            state.remux_proc.kill()  # the previous remux (old position or old film) holds the provider connection
            await state.remux_proc.wait()
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-user_agent", USER_AGENT,
            "-ss", f"{start:.3f}", "-i", state.vod_url(kind, item_id, ext),
            "-map", "0:v:0", "-map", f"0:a:{audio}?", "-c:v", "copy", *tag,
            "-c:a", "aac", "-ac", "2", "-b:a", "192k", "-sn", "-dn",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        state.remux_proc = proc
        resp = web.StreamResponse(status=200, headers={"Content-Type": "video/mp4", "Cache-Control": "no-cache"})
        try:
            first = await asyncio.wait_for(proc.stdout.read(256 * 1024), timeout=60)
            if not first:
                return web.Response(status=502, text="remux failed")
            await resp.prepare(request)
            await resp.write(first)
            while chunk := await proc.stdout.read(256 * 1024):
                await resp.write(chunk)
        except (ConnectionResetError, TimeoutError):
            pass  # viewer seeked, switched or closed the player
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        return resp


class VodFileView(_BaseView):
    """A film/episode file, seekable through Range requests: GET /vod/{movie|episode}/{id}.{ext}."""

    url = BASE + "/vod/{kind:movie|episode}/{item_id:\\d+}.{ext:[a-z0-9]+}"
    name = "api:iptv_proxy:vod_file"

    async def get(self, request: web.Request, kind: str, item_id: str, ext: str) -> web.StreamResponse:
        state = self.state
        if state is None:
            return web.Response(status=503, text="iptv_proxy not configured")
        if ext not in VOD_EXTENSIONS:
            return web.Response(status=404)
        return await self._file(request, state.vod_url(kind, item_id, ext), ext)


class SubtitleView(_BaseView):
    """External subtitle file from the provider's info, converted to WebVTT: GET ?u=<url>."""

    url = BASE + "/sub"
    name = "api:iptv_proxy:sub"

    async def get(self, request: web.Request) -> web.StreamResponse:
        state = self.state
        url = request.query.get("u", "")
        if state is None or not host_allowed(url, state.allowed_hosts, state.domain):
            return web.Response(status=403)
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(url, headers=HEADERS, timeout=ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return web.Response(status=502, text=f"upstream status {resp.status}")
                raw = await resp.content.read(5 * 1024 * 1024)
        except (ClientError, TimeoutError):
            return web.Response(status=504, text="upstream unreachable")
        text = raw.decode("utf-8") if _is_utf8(raw) else raw.decode("cp1252", "replace")
        return web.Response(
            text=srt_to_vtt(text), content_type="text/vtt", headers={"Cache-Control": "private, max-age=86400"}
        )


def _is_utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
