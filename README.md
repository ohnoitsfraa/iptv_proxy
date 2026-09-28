# IPTV Proxy for Home Assistant

A small custom integration that serves an **Xtream Codes** IPTV provider's live channels, channel logos and programme guide **through Home Assistant**.

Many providers only serve plain `http://`. Browsers block that inside an `https://` Home Assistant page (Nabu Casa, your own domain, the mobile app away from home). This proxy passes the provider's HLS streams through Home Assistant's own authenticated https connection. Nothing is transcoded, so it runs fine on a Raspberry Pi.

```
Browser / HA app ──https──▶ Home Assistant ──http──▶ IPTV provider
   (hls.js / Safari)       /api/iptv_proxy/…         (Xtream Codes)
```

## Features

- **Live channels over https.** HLS playlists are rewritten so every segment goes through the proxy. It works at home, remotely via Nabu Casa, and in the companion app.
- **No transcoding.** Bytes are passed through unchanged, which costs a Pi 4 a few percent CPU at most. Streams with B-frames play fine, unlike go2rtc/WebRTC.
- **Login stays on the server.** The Xtream username and password are entered once in the Home Assistant UI and stored in the config entry. They never appear in dashboards, URLs or the browser.
- **Authenticated endpoints.** Every endpoint requires Home Assistant auth. The dashboard uses signed paths (`auth/sign_path`), and segment URLs in playlists are signed by the server with a short lifetime.
- **Not an open proxy.** Only hosts handed out by the provider, or the provider's own domain (for example its picon/logo server), are fetched.
- **Channel logos.** The provider's logo server is proxied too, so logos load over https.
- **Programme guide (EPG).**
  - What's on now and next for many channels in one request, plus a longer list for the channel you're watching.
  - Base64 decoding and removal of duplicate or overlapping entries from merged guide sources.
  - Server-side caching (5 min / 15 min), with stale data served if the provider is unreachable.

## Requirements

- Home Assistant 2024.4 or newer. Developed and tested on 2026.9.
- An Xtream Codes account (server URL, username, password) from a provider you're legitimately subscribed to.

## Installation

1. Copy the files of this repository to `/config/custom_components/iptv_proxy/` on your Home Assistant, for example with the Studio Code Server or Filebrowser app, or over SSH/Samba:

   ```
   /config/custom_components/iptv_proxy/
   ├── __init__.py
   ├── config_flow.py
   ├── const.py
   ├── epg.py
   ├── manifest.json
   ├── playlist.py
   └── translations/
       ├── en.json
       └── nl.json
   ```

2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration**, search for **IPTV Proxy (HOME//OS)**, and enter:
   - **Server URL**, for example `http://line.example.net` (include the port if your provider uses one)
   - **Username**
   - **Password**

   The login is verified against the provider (`player_api.php`) before the entry is created.

**To update:** replace the files and restart Home Assistant. **To change the login:** remove the integration and add it again.

## Endpoints

All endpoints live under `/api/iptv_proxy` and require Home Assistant authentication: either a bearer token or a signed path from the `auth/sign_path` WebSocket command.

| Endpoint | Purpose |
|---|---|
| `GET /live/{stream_id}.m3u8` | Entry playlist for a live channel. Upstream: `/live/<user>/<pass>/{stream_id}.m3u8`. |
| `GET /pl?u=…` | Nested/variant playlists referenced by an upstream playlist (signed by the proxy). |
| `GET /seg?u=…` | Media segments, streamed through unchanged (signed by the proxy). |
| `GET /logo?u=<logo url>` | Channel logos from the provider's domain, cached by the browser for 1 day. |
| `GET /epg?ids=1,2,3[&full=1]` | Programme guide: `{ "<id>": [{ "title", "desc", "start", "end" }] }`. Up to 2 entries per channel, or 8 with `full=1`. Timestamps are Unix seconds. |

`stream_id` is the provider's numeric Xtream stream id. You can find ids with `player_api.php?…&action=get_live_streams`, or with any IPTV app that shows them.

### Example: playing a channel from a custom card

```js
// 1. get a signed, relative URL (valid 12 h) — no credentials involved
const { path } = await hass.callWS({
  type: 'auth/sign_path',
  path: '/api/iptv_proxy/live/1359.m3u8',
  expires: 12 * 3600,
});

// 2. play it with hls.js (or natively in Safari / iOS)
const hls = new Hls();
hls.loadSource(path);
hls.attachMedia(videoElement);

// 3. programme guide (hass.callApi adds the auth header)
const guide = await hass.callApi('GET', 'iptv_proxy/epg?ids=1359,1356');
```

## Dashboard card

This integration was built for the `tv-channels-card` of the HOME//OS dashboard. That card is registered separately, as a Lovelace resource. It shows:
- channel tiles grouped in tabs;
- one HLS player;
- the programme guide: what's on now on each tile, plus details and "coming up" below the player.

Minimal card config in proxy mode:

```yaml
type: custom:tv-channels-card
proxy: /api/iptv_proxy
channels:
  - { group: NL, name: NPO 1, id: "1359", logo: "http://picon.example.net/npo1.png" }
  - { group: BE, name: VRT 1, id: "1420208", logo: "http://picon.example.net/vrt1.png" }
```

`logo` may be the provider's original `http://` URL. The card loads it through `/logo`.

## Security notes

- Credentials are only used between Home Assistant and the provider. The entry playlist URL, which contains the username and password, is never sent to the browser. Rewritten playlists do contain the provider's *segment* URLs as a signed `u=` parameter. With typical Xtream servers these are tokenised paths without credentials, but check this for your own provider if it matters to you.
- `/pl`, `/seg` and `/logo` only fetch `http(s)` URLs whose host was handed out by the provider or belongs to the provider's domain. Other hosts get `403`.
- The signed segment paths the proxy writes into playlists expire after 15 minutes.
- Any authenticated Home Assistant user can use the endpoints, so treat this like any other integration that exposes a paid subscription.

## Limitations

- **Connection limit:** most Xtream accounts allow one concurrent connection. Watching on a second device, or switching channels very fast, can briefly return `upstream status 461/4xx`.
- **Data use:** remote viewing goes through Nabu Casa, or through your own reverse proxy, at roughly 1–3 GB per hour depending on the channel.
- **Latency:** playback runs 10–20 seconds behind live. That's normal for HLS and for the provider's own buffering.
- **Guide gaps:** the programme guide depends entirely on the provider. Event channels (for example PPV / DAZN events) often have none.

## Development

The playlist rewriting (`playlist.py`) and guide normalisation (`epg.py`) are pure Python without Home Assistant imports, so they can be unit-tested directly:

```python
from epg import normalize_listings
from playlist import rewrite_playlist

body, hosts = rewrite_playlist(text, final_url, "/api/iptv_proxy", sign=lambda p: p + "&authSig=test")
items = normalize_listings(epg_listings, now=time.time(), limit=4)
```

## Disclaimer

This project only relays streams you already have access to. Use it with a legitimate IPTV subscription and respect your provider's terms and local law. It is not affiliated with Home Assistant, Nabu Casa or any IPTV provider.
