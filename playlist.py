"""Pure helpers for rewriting HLS playlists (no Home Assistant imports, unit-testable)."""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import urlencode, urljoin, urlsplit

_URI_ATTR = re.compile(r'URI="([^"]+)"')


def rewrite_playlist(text: str, final_url: str, base: str, sign: Callable[[str], str]) -> tuple[str, set[str]]:
    """Point every URI in an HLS playlist at the proxy.

    Returns the new playlist and the set of upstream hosts it references.
    `sign` turns a proxy path (with query) into a signed path.
    """
    hosts: set[str] = set()

    def proxied(ref: str) -> str:
        absolute = urljoin(final_url, ref)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return ref
        hosts.add(parts.hostname.lower())
        kind = "pl" if parts.path.lower().endswith((".m3u8", ".m3u")) else "seg"
        return sign(f"{base}/{kind}?{urlencode({'u': absolute})}")

    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if "URI=" in s:
                s = _URI_ATTR.sub(lambda m: f'URI="{proxied(m.group(1))}"', s)
        else:
            s = proxied(s)
        out.append(s)
    return "\n".join(out) + "\n", hosts


def base_domain(host: str) -> str:
    """Last two labels of a hostname (tivi-ott.net for line.tivi-ott.net)."""
    labels = host.lower().rstrip(".").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host.lower()


def host_allowed(url: str, allowed_hosts: set[str], domain: str) -> bool:
    """Only proxy http(s) URLs on hosts the provider handed out, or on its own domain."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    h = parts.hostname.lower()
    return h in allowed_hosts or h == domain or h.endswith("." + domain)
