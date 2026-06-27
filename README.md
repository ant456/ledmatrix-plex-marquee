# Plex Marquee — LEDMatrix Plugin

A cinema marquee for your [LEDMatrix](https://github.com/ChuckBuilds/LEDMatrix) display that shows **Fanart.tv banner art** for whatever is playing on Plex — or rotates through a curated collection of banners you pick yourself.

Inspired by those backlit lightbox marquee signs in home theaters.

---

## How It Works

- **Playing** — when any Plex player starts, the plugin automatically fetches the banner for that title from Fanart.tv and displays it full-screen
- **Idle rotation** — when nothing is playing, it rotates through banners you've hand-picked via the web portal
- **Auto fallback** — if you haven't picked any banners yet, it auto-fetches banners for your recently added Plex titles

Banner images are cached locally after first fetch so they load instantly on repeat plays.

---

## Requirements

- [LEDMatrix](https://github.com/ChuckBuilds/LEDMatrix) installed and running
- Plex Media Server on your network
- Free API keys from:
  - **TMDB** — https://www.themoviedb.org (Settings → API → Bearer token)
  - **Fanart.tv** — https://fanart.tv/get-an-api-key/
- Home Assistant with Plex integration (for live playback detection)

---

## Installation

### 1. Copy the plugin

```bash
cp -r plex-marquee ~/LEDMatrix/plugin-repos/
echo "" > ~/LEDMatrix/plugin-repos/plex-marquee/requirements.txt
```

### 2. Add to `config/config.json`

```json
"plex-marquee": {
  "enabled": true,
  "display_duration": 30,
  "rotation_duration": 30,
  "live_priority": true,
  "ha_url": "http://192.168.0.x:8123",
  "ha_token": "your-ha-long-lived-token",
  "tmdb_token": "your-tmdb-bearer-token",
  "fanart_api_key": "your-fanart-api-key",
  "plex_url": "http://192.168.0.x:32400",
  "plex_token": "your-plex-token",
  "portal_port": 5009,
  "auto_recent_count": 20,
  "plex_entities": [
    "media_player.plex_your_player_name"
  ]
}
```

### 3. Restart

```bash
sudo systemctl restart ledmatrix
```

---

## Web Portal

Open in any browser on your network:

```
http://<PI_IP>:5009
```

**Search** your Plex library → **browse all Fanart.tv banners** for that title → **click to add** your favorite to the rotation.

From the rotation list you can:
- **Reorder** by dragging
- **Toggle** individual banners on/off
- **Delete** banners you no longer want

---

## Finding Your Plex Token

1. Open Plex Web and play anything
2. Click `···` → **Get Info** → **View XML**
3. Look for `X-Plex-Token=` in the URL bar
4. Copy the value — paste it into your config on the Pi only

---

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | bool | `true` | Enable/disable |
| `display_duration` | number | `30` | Seconds per rotation slot |
| `rotation_duration` | number | `30` | Seconds per banner when rotating |
| `live_priority` | bool | `true` | Take over display when Plex is playing |
| `ha_url` | string | — | Home Assistant URL |
| `ha_token` | string | — | HA long-lived access token |
| `tmdb_token` | string | — | TMDB API Bearer token |
| `fanart_api_key` | string | — | Fanart.tv API key |
| `plex_url` | string | — | Plex server URL |
| `plex_token` | string | — | Plex authentication token |
| `portal_port` | int | `5009` | Web portal port |
| `auto_recent_count` | int | `20` | Number of recent titles for auto fallback |
| `plex_entities` | array | — | HA Plex media_player entity IDs to monitor |

---

## License

GPL-3.0 — same as [LEDMatrix](https://github.com/ChuckBuilds/LEDMatrix).
