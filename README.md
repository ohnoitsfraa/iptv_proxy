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
- **Search.** Find any of the provider's live channels by name, or search the programme guide of your own channels by title (and description).
- **Films and series.** Search the provider's films and series, get details and episode lists, and play them through the proxy: as HLS when the provider offers it, otherwise as the original file with seeking (HTTP Range) support.
- **Audio browsers can't play.** Films with Dolby (AC3/E-AC3), DTS or TrueHD audio play silently in browsers. Those are remuxed on the fly with ffmpeg: video is copied untouched, only the audio is converted to AAC stereo, which costs a Raspberry Pi 4 little CPU. Multiple audio languages can be chosen.
- **Subtitles.** HLS subtitle tracks pass through the proxy, and external subtitle files from the provider's info are converted from SRT to WebVTT.
- **`iptv_proxy.inspect_vod` action.** Shows what a film or episode really contains (HLS availability and, via ffprobe, its video, audio and subtitle tracks).
- **`iptv_proxy.find_channels` action.** Looks up stream ids by channel name, so you don't need the Xtream API or a separate IPTV app to build your channel list.

## Requirements

- Home Assistant 2024.4 or newer. Developed and tested on 2026.9.
- An Xtream Codes account (server URL, username, password) from a provider you're legitimately subscribed to.

## Installation

### Via HACS (recommended)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ohnoitsfraa&repository=iptv_proxy&category=integration)

1. In HACS, open the **⋮** menu, choose **Custom repositories**, add `https://github.com/ohnoitsfraa/iptv_proxy` with type **Integration**, and click **Add**. The button above does the same in one click.
2. Search for **IPTV Proxy (HOME//OS)** in HACS and click **Download**.
3. Restart Home Assistant.

New versions show up as an update in HACS (and under **Settings → Updates**) when a new GitHub release is published.

### Manual

Copy `custom_components/iptv_proxy/` from this repository to `/config/custom_components/iptv_proxy/` on your Home Assistant, then restart Home Assistant.

### Setup

Go to **Settings → Devices & services → Add integration**, search for **IPTV Proxy (HOME//OS)**, and enter:

- **Server URL**, for example `http://line.example.net` (include the port if your provider uses one)
- **Username**
- **Password**

The login is verified against the provider (`player_api.php`) before the entry is created.

**To update:** install the new version via HACS (or replace the files) and restart Home Assistant. **To change the login:** remove the integration and add it again.

## Endpoints

All endpoints live under `/api/iptv_proxy` and require Home Assistant authentication: either a bearer token or a signed path from the `auth/sign_path` WebSocket command.

| Endpoint | Purpose |
|---|---|
| `GET /live/{stream_id}.m3u8` | Entry playlist for a live channel. Upstream: `/live/<user>/<pass>/{stream_id}.m3u8`. |
| `GET /pl?u=…` | Nested/variant playlists referenced by an upstream playlist (signed by the proxy). |
| `GET /seg?u=…` | Media segments, streamed through unchanged (signed by the proxy). |
| `GET /logo?u=<logo url>` | Channel logos from the provider's domain, cached by the browser for 1 day. |
| `GET /epg?ids=1,2,3[&full=1]` | Programme guide: `{ "<id>": [{ "title", "desc", "start", "end" }] }`. Up to 2 entries per channel, or 8 with `full=1`. Timestamps are Unix seconds. |
| `GET /streams?q=zdf[&limit=30]` | Search the provider's live channels: `[{ "id", "name", "group", "logo" }]`. Every word must appear in the name or category. The channel list is cached for 6 hours; adult categories are left out. |
| `GET /library?q=matrix[&limit=20]` | Search films and series: `{ "movies": [{ "id", "name", "group", "logo", "ext", "year" }], "series": [{ "id", "name", "group", "logo", "year" }] }`. Both lists are loaded on first use (they can be large) and cached for 6 hours. Without `q` it only starts loading them in the background. |
| `GET /movie/{id}` | Film details: `{ "name", "plot", "year", "minutes", "genre", "cover", "ext", "subtitles": [{ "lang", "url" }] }`. |
| `GET /series/{id}` | Series details with `seasons: [{ "season", "episodes": [{ "id", "ep", "title", "plot", "minutes", "ext", "subtitles" }] }]`. |
| `GET /vod/{movie\|episode}/{id}.m3u8` | HLS version of a film/episode, if the provider offers one (`502` otherwise). |
| `GET /vod/{movie\|episode}/{id}.{ext}` | The film/episode file, streamed unchanged; `Range` is forwarded so players can seek. |
| `GET /vod/{movie\|episode}/{id}.probe?ext=mkv` | Tracks of a film/episode via ffprobe (cached 24 h): `{ "container", "seconds", "video", "audio": [{ "codec", "channels", "language", "title" }], "subtitles": [...], "browser_audio" }`. |
| `GET /vod/{movie\|episode}/{id}.remux?ext=mkv&t=0&a=0` | The film/episode as fragmented MP4 with AAC stereo audio (video copied), starting at `t` seconds with audio track `a`. Not seekable by Range: request a new `t` to seek. One remux runs at a time. |
| `GET /sub?u=<subtitle url>` | An external subtitle file from the provider's info, as WebVTT. |
| `GET /search?q=journaal&ids=1,2,3` | Search the full guide of the given channels (max 120): `[{ "id", "title", "desc", "start", "end", "live" }]`, title matches first, then what's on now. Without `q` it only warms the guide cache. |

`stream_id` is the provider's numeric Xtream stream id. The easiest way to find ids is the `iptv_proxy.find_channels` action: in **Developer tools → Actions**, run it with for example `query: bbc news` and it returns matching channels with their `id`, `group` and `logo`. `player_api.php?…&action=get_live_streams`, or any IPTV app that shows ids, works too.

```yaml
action: iptv_proxy.find_channels
data:
  query: de zdf
  limit: 10
```

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

This integration was built for the **[TV Channels Card](https://github.com/ohnoitsfraa/tv-channels-card)**, which is also installable via HACS, as a custom repository of type *Dashboard*. It shows:
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

### Finding film ids

```yaml
action: iptv_proxy.find_vod
data:
  query: matrix
```

### Subtitles: what works

Browsers can only show subtitles they receive as WebVTT or as HLS subtitle tracks. So subtitles work when the provider offers an HLS version with subtitle tracks, lists separate subtitle files in its film info, or (in Safari) when an MP4 file carries text tracks. Subtitles embedded in MKV files can't be read by browsers; showing those would need server-side remuxing, which this integration doesn't do. Run `iptv_proxy.inspect_vod` on a film to see which case applies:

```yaml
action: iptv_proxy.inspect_vod
data:
  kind: movie
  id: "123456"
```

## Security notes

- Credentials are only used between Home Assistant and the provider. The entry playlist URL, which contains the username and password, is never sent to the browser. Rewritten playlists do contain the provider's *segment* URLs as a signed `u=` parameter. With typical Xtream servers these are tokenised paths without credentials, but check this for your own provider if it matters to you.
- `/pl`, `/seg` and `/logo` only fetch `http(s)` URLs whose host was handed out by the provider or belongs to the provider's domain. Other hosts get `403`.
- The signed segment paths the proxy writes into playlists expire after 15 minutes.
- Any authenticated Home Assistant user can use the endpoints, so treat this like any other integration that exposes a paid subscription.

## Limitations

- **Films and series:** large libraries (tens of thousands of titles) take a few seconds and some memory to load. Loading starts when the search panel opens, runs in the background and is refreshed every 6 hours without making searches wait. Seeking in a file opens a new upstream request; with a strict one-connection limit, very fast seeking can briefly fail.
- **Connection limit:** most Xtream accounts allow one concurrent connection. Watching on a second device, or switching channels very fast, can briefly return `upstream status 461/4xx`.
- **Data use:** remote viewing goes through Nabu Casa, or through your own reverse proxy, at roughly 1–3 GB per hour depending on the channel.
- **Latency:** playback runs 10–20 seconds behind live. That's normal for HLS and for the provider's own buffering.
- **Guide gaps:** the programme guide depends entirely on the provider. Event channels (for example PPV / DAZN events) often have none.

## Development

The playlist rewriting (`custom_components/iptv_proxy/playlist.py`) and guide normalisation (`epg.py`) are pure Python without Home Assistant imports, so they can be unit-tested directly from that folder:

```python
from epg import normalize_listings
from playlist import rewrite_playlist

body, hosts = rewrite_playlist(text, final_url, "/api/iptv_proxy", sign=lambda p: p + "&authSig=test")
items = normalize_listings(epg_listings, now=time.time(), limit=4)
```

## Disclaimer

This project only relays streams you already have access to. Use it with a legitimate IPTV subscription and respect your provider's terms and local law. It is not affiliated with Home Assistant, Nabu Casa or any IPTV provider.
