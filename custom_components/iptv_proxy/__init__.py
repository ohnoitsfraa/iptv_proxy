"""IPTV proxy: serves an Xtream Codes provider's HLS streams and logos through Home Assistant.

Only bytes are passed through (no transcoding). All endpoints require Home Assistant auth;
the dashboard uses signed paths, and playlists are rewritten to signed proxy paths.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import BASE, CONF_HOST, CONF_PASSWORD, CONF_USERNAME, DOMAIN, USER_AGENT
from .epg import normalize_listings
from .playlist import base_domain, host_allowed, rewrite_playlist

_LOGGER = logging.getLogger(__name__)

KEY_REFRESH_TOKEN_ID = "hass_refresh_token_id"
SEGMENT_SIGN_TTL = timedelta(minutes=15)
HEADERS = {"User-Agent": USER_AGENT}
EPG_TTL = {False: 300, True: 900}  # short (now/next) vs full-day guide, seconds
EPG_MAX_IDS = 80


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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the proxy from a config entry."""
    hass.data[DOMAIN] = _State(entry)
    if not hass.data.get(f"{DOMAIN}_views"):
        for view in (LiveView(hass), PlaylistView(hass), SegmentView(hass), LogoView(hass), EpgView(hass)):
            hass.http.register_view(view)
        hass.data[f"{DOMAIN}_views"] = True
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
        now = time.time()

        async def one(stream_id: str) -> tuple[str, list]:
            key = (stream_id, full)
            cached = state.epg_cache.get(key)
            if cached is None or now - cached[0] > EPG_TTL[full]:
                listings = await self._fetch(state, stream_id, full)
                if listings is None:  # upstream failed: serve stale data if we have it
                    listings = cached[1] if cached else []
                else:
                    state.epg_cache[key] = (now, listings)
            else:
                listings = cached[1]
            return stream_id, normalize_listings(listings, now, limit=8 if full else 2)

        results = await asyncio.gather(*(one(i) for i in ids))
        return self.json(dict(results), headers={"Cache-Control": "private, max-age=60"})

    async def _fetch(self, state: _State, stream_id: str, full: bool) -> list | None:
        params = {"username": state.username, "password": state.password, "stream_id": stream_id}
        params["action"] = "get_simple_data_table" if full else "get_short_epg"
        if not full:
            params["limit"] = "6"
        session = async_get_clientsession(self.hass)
        async with state.epg_sem:
            try:
                async with session.get(
                    f"{state.host}/player_api.php", params=params, headers=HEADERS, timeout=ClientTimeout(total=20)
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
            except (ClientError, TimeoutError, ValueError):
                return None
        listings = data.get("epg_listings") if isinstance(data, dict) else None
        return listings if isinstance(listings, list) else []
