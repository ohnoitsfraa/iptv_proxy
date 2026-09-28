"""IPTV proxy: serves an Xtream Codes provider's HLS streams and logos through Home Assistant.

Only bytes are passed through (no transcoding). All endpoints require Home Assistant auth;
the dashboard uses signed paths, and playlists are rewritten to signed proxy paths.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import BASE, CONF_HOST, CONF_PASSWORD, CONF_USERNAME, DOMAIN, USER_AGENT
from .playlist import base_domain, host_allowed, rewrite_playlist

_LOGGER = logging.getLogger(__name__)

KEY_REFRESH_TOKEN_ID = "hass_refresh_token_id"
SEGMENT_SIGN_TTL = timedelta(minutes=15)
HEADERS = {"User-Agent": USER_AGENT}


class _State:
    """Runtime state shared by the views."""

    def __init__(self, entry: ConfigEntry) -> None:
        self.host = entry.data[CONF_HOST].rstrip("/")
        self.username = entry.data[CONF_USERNAME]
        self.password = entry.data[CONF_PASSWORD]
        self.domain = base_domain(urlsplit(self.host).hostname or "")
        self.allowed_hosts: set[str] = {(urlsplit(self.host).hostname or "").lower()}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the proxy from a config entry."""
    hass.data[DOMAIN] = _State(entry)
    if not hass.data.get(f"{DOMAIN}_views"):
        for view in (LiveView(hass), PlaylistView(hass), SegmentView(hass), LogoView(hass)):
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
