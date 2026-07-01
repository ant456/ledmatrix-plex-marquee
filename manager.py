"""
Plex Marquee Plugin for LEDMatrix

A cinema marquee that rotates through Fanart.tv banner art.

Modes:
  1. Live — when Plex is playing, shows that title's chosen banner (live priority)
  2. Curated rotation — banners you've picked via the web portal
  3. Auto fallback — recently added from Plex library if no curated banners

Web portal (port 5009):
  - Search your Plex library
  - Browse ALL Fanart.tv banners for each title
  - Click to add your preferred banner to the rotation
  - Reorder, toggle, delete

Config:
  plex_url        — e.g. http://192.168.0.105:32400
  plex_token      — your Plex token (X-Plex-Token)
  tmdb_token      — TMDB API Read Access Token (Bearer token — NOT the v3 API key).
                    Get it at: themoviedb.org → Settings → API → API Read Access Token.
                    It starts with "eyJ..."
  fanart_api_key  — Fanart.tv API key
  ha_url          — Home Assistant URL
  ha_token        — HA long-lived access token
  plex_entities   — list of HA Plex media_player entity IDs
  portal_port     — web portal port (default 5009)
  rotation_duration — seconds per banner when rotating (default 30)
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from src.plugin_system.base_plugin import BasePlugin

try:
    from flask import Flask, request, jsonify, Response, send_file
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════════
# Data
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class BannerEntry:
    id:        str          # unique id
    title:     str          # display title
    url:       str          # fanart.tv image URL
    tmdb_id:   int = 0
    media_type: str = "movie"
    enabled:   bool = True
    added:     float = field(default_factory=time.time)


@dataclass
class PlayingState:
    title:      str   = ""
    year:       str   = ""
    media_type: str   = "movie"
    is_playing: bool  = False
    position:   float = 0.0
    duration:   float = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Plugin
# ═══════════════════════════════════════════════════════════════════════════════

class PlexMarqueePlugin(BasePlugin):

    TMDB_BASE   = "https://api.themoviedb.org/3"
    FANART_BASE = "https://webservice.fanart.tv/v3"

    def __init__(self, plugin_id, config, display_manager, cache_manager, plugin_manager):
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)

        # Config
        self.plex_url:       str  = config.get("plex_url", "").rstrip("/")
        self.plex_token:     str  = config.get("plex_token", "")
        self.tmdb_token:     str  = config.get("tmdb_token", "")
        self.fanart_api_key: str  = config.get("fanart_api_key", "")
        self.ha_url:         str  = config.get("ha_url", "").rstrip("/")
        self.ha_token:       str  = config.get("ha_token", "")
        self.plex_entities: List[str] = config.get("plex_entities", [])
        self.portal_port:      int   = int(config.get("portal_port", 5009))
        self.rotation_duration: float = float(config.get("rotation_duration", 30))
        self.auto_recent_count: int   = int(config.get("auto_recent_count", 20))

        self.W: int = display_manager.width
        self.H: int = display_manager.height

        # Paths
        self.plugin_dir   = Path(__file__).parent
        self.cache_dir    = self.plugin_dir / "cache"
        self.library_path = self.plugin_dir / "library.json"
        self.cache_dir.mkdir(exist_ok=True)

        # Library — curated banner rotation
        self._lib_lock = threading.Lock()
        self._library: List[BannerEntry] = self._load_library()

        # Image cache: url -> PIL Image scaled to display
        self._img_cache: Dict[str, Optional[Image.Image]] = {}
        self._img_lock   = threading.Lock()

        # TVDB ID cache: tmdb_id -> tvdb_id (TV shows only)
        self._tvdb_cache: Dict[int, Optional[int]] = {}

        # Playback state
        self._playing      = PlayingState()
        self._play_lock    = threading.Lock()
        self._play_banner: Optional[Image.Image] = None
        self._play_title   = ""
        self._play_tmdb_id = 0
        self._fetching     = False

        # Rotation state
        self._rot_idx:   int   = 0
        self._rot_start: float = 0.0
        self._cur_img:   Optional[Image.Image] = None

        # Auto-fallback cache
        self._auto_banners: List[BannerEntry] = []
        self._auto_fetched: float = 0.0

        self._last_poll: float = 0.0
        self._cycle_done: bool = False

        # Fonts
        self._font_sm = self._load_font(6)

        if FLASK_AVAILABLE:
            self._start_portal()

        self.logger.info("Plex Marquee ready  W=%d H=%d  portal=:%d  library=%d",
                         self.W, self.H, self.portal_port, len(self._library))

    # ── Font ─────────────────────────────────────────────────────────────────

    def _load_font(self, size: int) -> ImageFont.ImageFont:
        p = Path("/home/ledpi/LEDMatrix/assets/fonts/PressStart2P-Regular.ttf")
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                pass
        return ImageFont.load_default()

    # ── Library ───────────────────────────────────────────────────────────────

    def _load_library(self) -> List[BannerEntry]:
        if not self.library_path.exists():
            return []
        try:
            return [BannerEntry(**e) for e in json.loads(self.library_path.read_text())]
        except Exception:
            return []

    def _save_library(self) -> None:
        try:
            self.library_path.write_text(
                json.dumps([asdict(e) for e in self._library], indent=2))
        except Exception as e:
            self.logger.error("Save library: %s", e)

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get_json(self, url: str, headers: Dict = {}) -> Optional[Dict]:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read())
        except Exception as e:
            self.logger.debug("GET %s: %s", url, e)
            return None

    def _get_image(self, url: str) -> Optional[bytes]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LEDMatrix-PlexMarquee/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read()
        except Exception as e:
            self.logger.debug("IMG %s: %s", url, e)
            return None

    # ── HA — find active Plex player ─────────────────────────────────────────

    def _find_active_plex(self) -> Optional[Dict]:
        if not self.ha_url or not self.ha_token:
            return None
        hdrs = {"Authorization": f"Bearer {self.ha_token}", "Content-Type": "application/json"}
        for eid in self.plex_entities:
            data = self._get_json(f"{self.ha_url}/api/states/{eid}", hdrs)
            if data and data.get("state") in ("playing", "paused"):
                return data
        return None

    # ── Plex API ──────────────────────────────────────────────────────────────

    def _plex_xml(self, path: str, extra_params: str = "") -> Optional[Any]:
        """Fetch Plex API and parse XML response."""
        import xml.etree.ElementTree as ET
        if not self.plex_url:
            return None
        sep = "&" if "?" in path else "?"
        url = f"{self.plex_url}{path}{sep}X-Plex-Token={self.plex_token}{extra_params}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LEDMatrix/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                return ET.fromstring(r.read())
        except Exception as e:
            self.logger.debug("Plex XML %s: %s", path, e)
            return None

    def _plex_search(self, query: str) -> List[Dict]:
        """Search Plex library — returns list of {title, year, type}."""
        enc  = urllib.parse.quote(query)
        root = self._plex_xml(f"/search?query={enc}")
        if root is None:
            return []
        results = []
        for item in root:
            t = item.get("type", "")
            if t not in ("movie", "show"):
                continue
            results.append({
                "title": item.get("title", ""),
                "year":  str(item.get("year", "")),
                "type":  t,
            })
        return results[:20]

    def _plex_recent(self) -> List[Dict]:
        """Get recently added movies and shows from Plex."""
        results = []
        for section_type, plex_type in [("movie", "1"), ("show", "2")]:
            root = self._plex_xml(f"/library/recentlyAdded?type={plex_type}")
            if root is None:
                continue
            for item in root:
                t = item.get("type", "")
                if t not in ("movie", "show", "season", "episode"):
                    continue
                title = item.get("title", "")
                year  = str(item.get("year", ""))
                if title:
                    results.append({"title": title, "year": year, "type": section_type})
        return results[:self.auto_recent_count]

    # ── TMDB ──────────────────────────────────────────────────────────────────

    def _tmdb_id(self, title: str, year: str, media_type: str) -> Optional[int]:
        if not self.tmdb_token:
            self.logger.warning("tmdb_token not configured -- must be the TMDB API Read Access Token (starts with eyJ), NOT the v3 API key. Get it at: themoviedb.org -> Settings -> API -> API Read Access Token")
            return None
        hdrs = {"Authorization": f"Bearer {self.tmdb_token}", "Accept": "application/json"}
        stype = "movie" if media_type == "movie" else "tv"
        params = {"query": title, "language": "en-US", "page": 1}
        if year:
            params["year"] = year
        qs   = urllib.parse.urlencode(params)
        data = self._get_json(f"{self.TMDB_BASE}/search/{stype}?{qs}", hdrs)
        if data and data.get("results"):
            return data["results"][0].get("id")
        # retry without year
        if year:
            del params["year"]
            qs   = urllib.parse.urlencode(params)
            data = self._get_json(f"{self.TMDB_BASE}/search/{stype}?{qs}", hdrs)
            if data and data.get("results"):
                return data["results"][0].get("id")
        return None

    def _tvdb_id(self, tmdb_id: int) -> Optional[int]:
        """Resolve TMDB TV ID -> TVDB ID via TMDB external_ids endpoint.
        Fanart.tv /tv/ endpoint requires TVDB ID, not TMDB ID."""
        if tmdb_id in self._tvdb_cache:
            return self._tvdb_cache[tmdb_id]
        if not self.tmdb_token:
            self._tvdb_cache[tmdb_id] = None
            return None
        hdrs = {"Authorization": f"Bearer {self.tmdb_token}", "Accept": "application/json"}
        data = self._get_json(f"{self.TMDB_BASE}/tv/{tmdb_id}/external_ids", hdrs)
        tvdb = int(data["tvdb_id"]) if data and data.get("tvdb_id") else None
        self._tvdb_cache[tmdb_id] = tvdb
        if tvdb:
            self.logger.debug("TVDB ID for tmdb=%d -> tvdb=%d", tmdb_id, tvdb)
        else:
            self.logger.warning("No TVDB ID found for tmdb_id=%d", tmdb_id)
        return tvdb

    # ── Fanart.tv ─────────────────────────────────────────────────────────────

    def _fanart_all_banners(self, tmdb_id: int, media_type: str) -> List[Dict]:
        """Return ALL banner/background URLs from Fanart.tv for a title.
        Movies use TMDB ID directly. TV shows require TVDB ID."""
        if media_type == "movie":
            fanart_id = tmdb_id
            endpoint  = "movies"
        else:
            # Fanart.tv /tv/ requires TVDB ID, not TMDB ID
            fanart_id = self._tvdb_id(tmdb_id)
            endpoint  = "tv"
            if not fanart_id:
                self.logger.warning(
                    "Cannot fetch Fanart.tv banners for tmdb_id=%d — no TVDB ID", tmdb_id)
                return []

        url  = f"{self.FANART_BASE}/{endpoint}/{fanart_id}?api_key={self.fanart_api_key}"
        data = self._get_json(url, {"User-Agent": "LEDMatrix-PlexMarquee/1.0"})
        if not data:
            return []

        banners = []
        if media_type == "movie":
            keys = ["moviebanner", "moviebackground", "moviethumb"]
        else:
            keys = ["tvbanner", "showbackground", "seasonbanner"]

        for key in keys:
            for item in data.get(key, []):
                img_url = item.get("url", "")
                if img_url:
                    banners.append({
                        "url":   img_url,
                        "type":  key,
                        "lang":  item.get("lang", ""),
                        "likes": int(item.get("likes", 0)),
                    })

        # Sort: English first, then by likes
        banners.sort(key=lambda x: (x["lang"] != "en", -x["likes"]))
        return banners

    def _fanart_best_banner(self, tmdb_id: int, media_type: str) -> Optional[str]:
        """Return the single best banner URL."""
        banners = self._fanart_all_banners(tmdb_id, media_type)
        return banners[0]["url"] if banners else None

    # ── Image loading & caching ───────────────────────────────────────────────

    def _load_scaled(self, url: str) -> Optional[Image.Image]:
        with self._img_lock:
            if url in self._img_cache:
                return self._img_cache[url]

        data = self._get_image(url)
        if not data:
            with self._img_lock:
                self._img_cache[url] = None
            return None

        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            img = img.resize((self.W, self.H), Image.LANCZOS)
        except Exception:
            with self._img_lock:
                self._img_cache[url] = None
            return None

        with self._img_lock:
            self._img_cache[url] = img
        return img

    def _load_scaled_cached(self, url: str, cache_key: str) -> Optional[Image.Image]:
        """Load image, using disk cache if available."""
        safe  = re.sub(r'[^a-zA-Z0-9_-]', '_', cache_key)[:60]
        cpath = self.cache_dir / f"{safe}.jpg"

        if cpath.exists():
            try:
                img = Image.open(str(cpath)).convert("RGB")
                img = img.resize((self.W, self.H), Image.LANCZOS)
                with self._img_lock:
                    self._img_cache[url] = img
                return img
            except Exception:
                pass

        img = self._load_scaled(url)
        if img:
            try:
                img.save(str(cpath), "JPEG", quality=85)
            except Exception:
                pass
        return img

    # ── Playback detection ────────────────────────────────────────────────────

    def _update_playing(self) -> None:
        active = self._find_active_plex()
        if not active:
            with self._play_lock:
                self._playing.is_playing = False
            return

        attrs  = active.get("attributes", {})
        raw    = str(attrs.get("media_series_title") or attrs.get("media_title") or "")
        year   = str(attrs.get("media_year", "") or "")
        m_type = "tv" if attrs.get("media_series_title") else "movie"

        # Strip year from title
        year_m = re.search(r'\((\d{4})\)\s*$', raw)
        title  = re.sub(r'\s*\(\d{4}\)\s*$', '', raw).strip()
        if not year and year_m:
            year = year_m.group(1)

        with self._play_lock:
            self._playing.title      = title
            self._playing.year       = year
            self._playing.media_type = m_type
            self._playing.is_playing = active.get("state") == "playing"
            self._playing.position   = float(attrs.get("media_position", 0) or 0)
            self._playing.duration   = float(attrs.get("media_duration", 0) or 0)

        # Fetch banner for current title if changed
        if title and title != self._play_title and not self._fetching:
            self._play_title = title
            threading.Thread(target=self._fetch_play_banner,
                             args=(title, year, m_type), daemon=True).start()

    def _fetch_play_banner(self, title: str, year: str, media_type: str) -> None:
        self._fetching = True
        try:
            tmdb_id = self._tmdb_id(title, year, media_type)
            if not tmdb_id:
                self.logger.warning("No TMDB ID for '%s'", title)
                return
            self._play_tmdb_id = tmdb_id

            # Check if user has a preferred banner for this title in library
            with self._lib_lock:
                preferred = next(
                    (e for e in self._library if e.tmdb_id == tmdb_id and e.enabled), None
                )

            if preferred:
                img = self._load_scaled_cached(preferred.url, f"play_{tmdb_id}")
            else:
                url = self._fanart_best_banner(tmdb_id, media_type)
                if not url:
                    return
                img = self._load_scaled_cached(url, f"play_{tmdb_id}")

            if img:
                with self._play_lock:
                    self._play_banner = img
                self.logger.info("Play banner ready: %s", title)
        finally:
            self._fetching = False

    # ── Auto-fallback banners ─────────────────────────────────────────────────

    def _refresh_auto_banners(self) -> None:
        """Fetch banners for recently added Plex content."""
        if not self.plex_token:
            return
        recent  = self._plex_recent()
        banners = []
        for item in recent:
            t     = item["title"]
            y     = item["year"]
            mtype = "movie" if item["type"] == "movie" else "tv"
            tid   = self._tmdb_id(t, y, mtype)
            if not tid:
                continue
            url = self._fanart_best_banner(tid, mtype)
            if not url:
                continue
            banners.append(BannerEntry(
                id=f"auto_{tid}", title=t, url=url,
                tmdb_id=tid, media_type=mtype
            ))
        self._auto_banners  = banners
        self._auto_fetched  = time.time()
        self.logger.info("Auto banners: %d titles", len(banners))

    # ── Rotation ──────────────────────────────────────────────────────────────

    def _active_rotation(self) -> List[BannerEntry]:
        """Return curated library entries or auto banners as fallback."""
        with self._lib_lock:
            curated = [e for e in self._library if e.enabled]
        if curated:
            return curated
        return [e for e in self._auto_banners if e.enabled]

    def _advance_rotation(self) -> Optional[Image.Image]:
        """Advance to the next banner in rotation, return the image."""
        entries = self._active_rotation()
        if not entries:
            return None

        now = time.time()
        if now - self._rot_start >= self.rotation_duration or self._cur_img is None:
            self._rot_idx   = (self._rot_idx + 1) % len(entries)
            self._rot_start = now
            entry = entries[self._rot_idx]
            img   = self._load_scaled_cached(entry.url, f"rot_{entry.id}")
            if img:
                self._cur_img = img

        return self._cur_img

    # ── BasePlugin ────────────────────────────────────────────────────────────

    def update(self) -> None:
        now = time.time()
        if now - self._last_poll >= 15:
            self._last_poll = now
            threading.Thread(target=self._update_playing, daemon=True).start()

            # Refresh auto banners every 6 hours
            if now - self._auto_fetched > 21600:
                threading.Thread(target=self._refresh_auto_banners, daemon=True).start()

    def has_live_content(self) -> bool:
        with self._play_lock:
            return self._playing.is_playing

    def display(self, force_clear: bool = False) -> None:
        try:
            with self._play_lock:
                is_playing  = self._playing.is_playing
                play_banner = self._play_banner

            dm = self.display_manager

            if is_playing and play_banner:
                dm.image.paste(play_banner, (0, 0))
            else:
                img = self._advance_rotation()
                if img:
                    dm.image.paste(img, (0, 0))
                else:
                    dm.draw.rectangle([0, 0, self.W-1, self.H-1], fill=(0, 0, 0))

            dm.update_display()
            self._cycle_done = True

        except Exception as e:
            import traceback
            self.logger.error("display() error: %s\n%s", e, traceback.format_exc())

    def is_cycle_complete(self) -> bool:
        return self._cycle_done

    def reset_cycle_state(self) -> None:
        self._cycle_done = False

    def get_display_duration(self) -> float:
        return self.rotation_duration

    # ── Web portal ────────────────────────────────────────────────────────────

    def _start_portal(self) -> None:
        app = Flask(f"plex_marquee_{self.plugin_id}")
        app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024

        @app.route("/", methods=["GET"])
        def index():
            static = self.plugin_dir / "static" / "portal.html"
            return Response(
                static.read_text() if static.exists()
                else "<h1>Plex Marquee Portal</h1><p>portal.html missing</p>",
                mimetype="text/html"
            )

        @app.route("/api/search", methods=["GET"])
        def search():
            q = request.args.get("q", "").strip()
            if not q:
                return jsonify([])
            results = self._plex_search(q)
            return jsonify(results)

        @app.route("/api/banners", methods=["GET"])
        def banners():
            """Return all Fanart.tv banners for a title."""
            title      = request.args.get("title", "").strip()
            year       = request.args.get("year", "").strip()
            media_type = request.args.get("type", "movie").strip()
            if not title:
                return jsonify({"error": "title required"}), 400

            tmdb_id = self._tmdb_id(title, year, media_type)
            if not tmdb_id:
                return jsonify({"error": f"Title not found on TMDB: {title}"}), 404

            all_banners = self._fanart_all_banners(tmdb_id, media_type)
            return jsonify({
                "tmdb_id":    tmdb_id,
                "title":      title,
                "media_type": media_type,
                "banners":    all_banners,
            })

        @app.route("/api/library", methods=["GET"])
        def get_library():
            with self._lib_lock:
                return jsonify([asdict(e) for e in self._library])

        @app.route("/api/library", methods=["POST"])
        def add_to_library():
            data = request.get_json(force=True) or {}
            entry = BannerEntry(
                id=f"{data.get('tmdb_id', 0)}_{int(time.time())}",
                title=str(data.get("title", "")),
                url=str(data.get("url", "")),
                tmdb_id=int(data.get("tmdb_id", 0)),
                media_type=str(data.get("media_type", "movie")),
            )
            if not entry.url or not entry.title:
                return jsonify({"error": "title and url required"}), 400
            with self._lib_lock:
                self._library.append(entry)
                self._save_library()
            return jsonify(asdict(entry)), 200

        @app.route("/api/library/<entry_id>/toggle", methods=["POST"])
        def toggle(entry_id):
            with self._lib_lock:
                for e in self._library:
                    if e.id == entry_id:
                        e.enabled = not e.enabled
                        self._save_library()
                        return jsonify(asdict(e))
            return jsonify({"error": "not found"}), 404

        @app.route("/api/library/<entry_id>", methods=["DELETE"])
        def delete(entry_id):
            with self._lib_lock:
                self._library = [e for e in self._library if e.id != entry_id]
                self._save_library()
            return jsonify({"status": "deleted"})

        @app.route("/api/reorder", methods=["POST"])
        def reorder():
            order = (request.get_json(force=True) or {}).get("order", [])
            with self._lib_lock:
                by_id = {e.id: e for e in self._library}
                self._library = [by_id[i] for i in order if i in by_id]
                for e in by_id.values():
                    if e.id not in order:
                        self._library.append(e)
                self._save_library()
            return jsonify({"status": "ok"})

        @app.route("/api/preview", methods=["GET"])
        def preview():
            """Proxy a Fanart.tv image for preview in the portal."""
            url = request.args.get("url", "")
            if not url or "fanart.tv" not in url:
                return jsonify({"error": "invalid url"}), 400
            data = self._get_image(url)
            if not data:
                return jsonify({"error": "fetch failed"}), 502
            return Response(data, mimetype="image/jpeg")

        def _run():
            import logging as _l
            _l.getLogger("werkzeug").setLevel(_l.ERROR)
            app.run(host="0.0.0.0", port=self.portal_port,
                    debug=False, use_reloader=False)

        threading.Thread(target=_run, daemon=True, name="marquee-portal").start()
        self.logger.info("Plex Marquee portal started on port %d", self.portal_port)

    def validate_config(self) -> bool:
        if not super().validate_config():
            return False
        if not self.fanart_api_key:
            self.logger.error("fanart_api_key is required")
            return False
        if not self.tmdb_token:
            self.logger.error("tmdb_token is required -- must be the TMDB API Read Access Token (starts with eyJ), NOT the v3 API key. Get it at: themoviedb.org -> Settings -> API -> API Read Access Token")
            return False
        return True
