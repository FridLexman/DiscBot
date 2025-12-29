from __future__ import annotations

import asyncio
import base64
import itertools
import logging
import os
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Deque, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands

import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from branding import build_logo_file, logo_embed_url, logo_requires_attachment

log = logging.getLogger(__name__)

# =========================
# Configuration (via env)
# =========================
FFMPEG_EXE = os.getenv("FFMPEG_EXE", "ffmpeg")

# Caps (avoid monster queues)
PLAYLIST_MAX = int(os.getenv("MUSIC_PLAYLIST_MAX", "50"))       # YouTube playlist item cap
SPOTIFY_MAX = int(os.getenv("MUSIC_SPOTIFY_MAX", "100"))        # Spotify playlist/album item cap

# UI refresher cadence (seconds)
PROGRESS_UPDATE_SECONDS = float(os.getenv("MUSIC_PROGRESS_UPDATE_SECONDS", "5"))

# Panel cleanup + bumping
DELETE_PANEL_ON_IDLE = os.getenv("MUSIC_DELETE_PANEL_ON_IDLE", "0") == "1"
PANEL_BUMP_SECONDS = int(os.getenv("MUSIC_PANEL_BUMP_SECONDS", "45"))  # 0 disables bumping
MUSIC_LAST_PLAYED_SECONDS = int(os.getenv("MUSIC_LAST_PLAYED_SECONDS", "20"))  # keep last track visible

# yt-dlp robustness knobs
YT_PO_TOKEN = os.getenv("YT_PO_TOKEN", "").strip()  # optional Android PO token (android.gvs+XXXX)
YTDL_SOCKET_TIMEOUT = float(os.getenv("YTDL_SOCKET_TIMEOUT", "20"))
YTDL_MAX_RETRIES = int(os.getenv("YTDL_MAX_RETRIES", "2"))

# Cookies sources (choose one or none)
YTDLP_COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()  # e.g. "chrome" or "firefox"
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "").strip()                  # path to Netscape txt
YTDLP_COOKIES_B64 = os.getenv("YTDLP_COOKIES_B64", "").strip()                    # base64 of Netscape txt (temp-file it)

GLOBAL_MUSIC_PLAYERS: dict[int, "GuildPlayer"] = {}

SPOTIFY_URL_RE = re.compile(r"https?://open\.spotify\.com/(track|album|playlist)/([A-Za-z0-9]+)")


# =========================
# Helpers
# =========================
def fmt_time(seconds: Optional[int]) -> str:
    if seconds is None or seconds < 0:
        return "live"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


@dataclass
class Track:
    seq: Optional[int] = None
    title: str = ""
    stream_url: str = ""
    webpage_url: str = ""
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    requested_by: Optional[discord.Member] = None


# =========================
# yt-dlp base options
# =========================
def build_base_ytdl_opts() -> Dict[str, Any]:
    extractor_args = {"youtube": {"player_client": ["android", "web"]}}
    if YT_PO_TOKEN:
        extractor_args["youtube"]["po_token"] = YT_PO_TOKEN  # android.gvs+XXXX

    opts: Dict[str, Any] = {
        "format": "bestaudio/best",
        "quiet": True,
        "noplaylist": True,                # flipped for playlists
        "default_search": "ytsearch",
        "skip_download": True,
        "cachedir": False,
        "retries": 3,
        "socket_timeout": YTDL_SOCKET_TIMEOUT,
        "extractor_args": extractor_args,
    }
    # Cookies (browser/file are static; B64 handled per-player)
    if YTDLP_COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (YTDLP_COOKIES_FROM_BROWSER,)
    elif YTDLP_COOKIES_FILE:
        opts["cookiefile"] = YTDLP_COOKIES_FILE
    return opts


BASE_YTDL_OPTS = build_base_ytdl_opts()


# =========================
# Async wrappers (keep loop snappy)
# =========================
async def _ydl_extract_async(url: str, opts: dict):
    def _do():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    return await asyncio.to_thread(_do)

async def _extract_with_retries(url: str, opts: dict, *, tries: int = None):
    tries = YTDL_MAX_RETRIES if tries is None else tries
    last_err = None
    for _ in range(tries + 1):
        try:
            return await _ydl_extract_async(url, opts)
        except Exception as e:
            last_err = e
    raise last_err


# =========================
# Cookie temp-file (for B64)
# =========================
def write_temp_cookie_file_from_b64(b64: str) -> Path:
    data = base64.b64decode(b64)
    fd, path = tempfile.mkstemp(prefix="yt_cookies_", suffix=".txt")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return Path(path)


# =========================
# YouTube helpers (async + robust)
# =========================
def _is_yt_playlist(url: str) -> bool:
    u = url.lower()
    return ("youtube.com/playlist" in u) or ("list=" in u and ("youtube.com" in u or "youtu.be/" in u))

async def yt_extract(url: str, *, ytdl_opts: dict) -> Track:
    info = await _extract_with_retries(url, ytdl_opts)
    if info and "entries" in info:
        info = info["entries"][0]
    if not info:
        raise RuntimeError("Could not extract YouTube info")

    def _pick_stream(info_obj):
        stream = info_obj.get("url")
        if stream:
            return stream
        fmts = info_obj.get("formats") or []
        best = max(
            (f for f in fmts if f.get("acodec") not in (None, "none")),
            key=lambda f: f.get("abr") or 0,
            default=None,
        )
        return best.get("url") if best else None

    stream = _pick_stream(info)
    if not stream:
        web_only = dict(ytdl_opts)
        web_only["extractor_args"] = {"youtube": {"player_client": ["web"]}}
        info2 = await _extract_with_retries(url, web_only)
        if info2 and "entries" in info2:
            info2 = info2["entries"][0]
        stream = _pick_stream(info2 or {})

    if not stream:
        raise RuntimeError("No streamable audio found")

    thumbs = info.get("thumbnails") or []
    thumb = sorted(thumbs, key=lambda t: t.get("height", 0), reverse=True)[0]["url"] if thumbs else None
    return Track(
        seq=None,
        title=info.get("title") or "Unknown",
        stream_url=stream,
        webpage_url=info.get("webpage_url") or url,
        duration=info.get("duration"),
        thumbnail=thumb,
        requested_by=None,
    )

async def yt_search_first(query: str, *, ytdl_opts: dict) -> Track:
    return await yt_extract(f"ytsearch1:{query}", ytdl_opts=ytdl_opts)

async def yt_extract_playlist(url: str, *, ytdl_opts: dict) -> List[Track]:
    opts = dict(ytdl_opts)
    opts["noplaylist"] = False
    opts["extract_flat"] = True

    info = await _extract_with_retries(url, opts)
    if not info:
        raise RuntimeError("Could not extract playlist info")

    playlist_id = (info.get("id") or "") if isinstance(info, dict) else ""
    entries = info.get("entries") or []
    out: List[Track] = []

    def _normalize_url(entry: Dict[str, Any]) -> Optional[str]:
        vid = entry.get("id")
        page_url = entry.get("url") or entry.get("webpage_url")
        if page_url and page_url.startswith(("http://", "https://")):
            return page_url
        if vid:
            return f"https://www.youtube.com/watch?v={vid}" + (f"&list={playlist_id}" if playlist_id else "")
        return None

    for i, entry in enumerate(entries):
        if i >= PLAYLIST_MAX:
            break
        page_url = _normalize_url(entry)
        if not page_url:
            continue
        try:
            t = await yt_extract(page_url, ytdl_opts=ytdl_opts)
            out.append(t)
        except Exception:
            continue

    if not out:
        raise RuntimeError("Playlist contained no playable tracks")
    return out


# =========================
# Spotify helpers
# =========================
def make_spotify():
    cid = os.getenv("SPOTIFY_CLIENT_ID")
    csec = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not cid or not csec:
        return None
    auth = SpotifyClientCredentials(client_id=cid, client_secret=csec)
    return spotipy.Spotify(auth_manager=auth, requests_timeout=15, retries=3)

def _spotify_page_album_tracks(sp: spotipy.Spotify, album_id: str, limit: int = 50):
    fetched = 0
    offset = 0
    while True:
        page = sp.album_tracks(album_id, limit=limit, offset=offset)
        items = page.get("items", [])
        if not items:
            break
        for it in items:
            yield it
            fetched += 1
        if fetched >= SPOTIFY_MAX:
            break
        offset += len(items)
        if len(items) < limit:
            break

def _spotify_page_playlist_items(sp: spotipy.Spotify, playlist_id: str, limit: int = 100):
    fetched = 0
    offset = 0
    while True:
        page = sp.playlist_items(playlist_id, additional_types=("track",), limit=limit, offset=offset)
        items = page.get("items", [])
        if not items:
            break
        for it in items:
            yield it
            fetched += 1
        if fetched >= SPOTIFY_MAX:
            break
        offset += len(items)
        if len(items) < limit:
            break

async def create_tracks_from_query(query: str, *, ytdl_opts: dict) -> List[Track]:
    m = SPOTIFY_URL_RE.match(query)
    if m:
        kind, ident = m.groups()
        sp = make_spotify()
        if not sp:
            raise RuntimeError("Spotify credentials missing")
        results: List[Track] = []

        if kind == "track":
            t = sp.track(ident)
            results.append(await yt_search_first(f"{t['artists'][0]['name']} {t['name']} audio", ytdl_opts=ytdl_opts))

        elif kind == "album":
            count = 0
            for t in _spotify_page_album_tracks(sp, ident):
                q = f"{t['artists'][0]['name']} {t['name']} audio"
                results.append(await yt_search_first(q, ytdl_opts=ytdl_opts))
                count += 1
                if count >= SPOTIFY_MAX:
                    break

        elif kind == "playlist":
            count = 0
            for it in _spotify_page_playlist_items(sp, ident):
                t = it.get("track")
                if not t or t.get("is_local"):
                    continue
                artists = t.get("artists") or []
                artist_name = artists[0]["name"] if artists else ""
                q = f"{artist_name} {t['name']} audio"
                results.append(await yt_search_first(q, ytdl_opts=ytdl_opts))
                count += 1
                if count >= SPOTIFY_MAX:
                    break

        return results

    if _is_yt_playlist(query):
        return await yt_extract_playlist(query, ytdl_opts=ytdl_opts)

    if "youtube.com" in query or "youtu.be" in query:
        return [await yt_extract(query, ytdl_opts=ytdl_opts)]

    return [await yt_search_first(query, ytdl_opts=ytdl_opts)]


# =========================
# Control Buttons (persistent)
# =========================
class ControlView(discord.ui.View):
    def __init__(self, bot: commands.Bot, player: "GuildPlayer | None" = None):
        super().__init__(timeout=None)  # persistent
        self.bot = bot
        self.player = player
        self._sync_buttons()

    def _player(self, inter: discord.Interaction) -> "GuildPlayer":
        if self.player:
            return self.player
        cog: Music = self.bot.get_cog("Music")  # type: ignore
        return cog.get_player(inter.guild)  # type: ignore

    def _sync_buttons(self):
        prev_button = next((c for c in self.children if isinstance(c, discord.ui.Button) and c.custom_id == "music_prev"), None)
        if prev_button:
            p = self.player
            prev_button.disabled = not (p and p.can_go_previous())

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="music_prev")
    async def prev(self, inter: discord.Interaction, _: discord.ui.Button):
        p = self._player(inter)
        if not p.can_go_previous():
            await inter.response.defer()
            return
        p.play_previous()
        await inter.response.defer()
        await p.post_or_update_panel()

    @discord.ui.button(emoji="◼️", style=discord.ButtonStyle.danger, custom_id="music_stop")
    async def stop(self, inter: discord.Interaction, _: discord.ui.Button):
        p = self._player(inter)
        p.stop()
        await inter.response.defer()
        await p.post_or_update_panel()

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="music_toggle")
    async def toggle(self, inter: discord.Interaction, _: discord.ui.Button):
        p = self._player(inter)
        if p.voice and p.voice.is_playing():
            p.pause()
        elif p.voice and p.voice.is_paused():
            p.resume()
        await inter.response.defer()
        await p.post_or_update_panel()

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="music_next")
    async def next(self, inter: discord.Interaction, _: discord.ui.Button):
        p = self._player(inter)
        p.skip()
        await inter.response.defer()
        await p.post_or_update_panel()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="music_repeat")
    async def repeat(self, inter: discord.Interaction, _: discord.ui.Button):
        p = self._player(inter)
        nxt = {"off": "one", "one": "all", "all": "off"}[p.repeat_mode]
        p.repeat_mode = nxt
        await inter.response.defer()
        await p.post_or_update_panel()


class RepeatModeView(discord.ui.View):
    """Ephemeral controls that let users set repeat mode with explicit buttons."""

    def __init__(self, music_cog: "Music", guild: discord.Guild, current_mode: str, *, timeout: float = 45):
        super().__init__(timeout=timeout)
        self.music_cog = music_cog
        self.guild_id = guild.id
        self.current_mode = current_mode
        self._refresh_styles()

    def _player(self, guild: discord.Guild | None):
        if guild is None:
            raise RuntimeError("Guild context vanished for repeat selection.")
        return self.music_cog.get_player(guild)

    def _refresh_styles(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.style = (
                    discord.ButtonStyle.success
                    if child.custom_id == f"repeat_{self.current_mode}"
                    else discord.ButtonStyle.secondary
                )

    async def _set_mode(self, interaction: discord.Interaction, mode: str):
        player = self._player(interaction.guild)
        player.repeat_mode = mode
        self.current_mode = mode
        self._refresh_styles()

        content = f"Repeat mode locked to **{mode}**"
        if interaction.response.is_done():
            await interaction.edit_original_response(content=content, view=self)
        else:
            await interaction.response.edit_message(content=content, view=self)
        await player.post_or_update_panel()

    @discord.ui.button(label="Off", emoji="🚫", custom_id="repeat_off")
    async def btn_off(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._set_mode(interaction, "off")

    @discord.ui.button(label="One", emoji="🔂", custom_id="repeat_one")
    async def btn_one(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._set_mode(interaction, "one")

    @discord.ui.button(label="All", emoji="🔁", custom_id="repeat_all")
    async def btn_all(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._set_mode(interaction, "all")

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if getattr(self, "message", None):
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# =========================
# Player
# =========================
class GuildPlayer:
    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: Deque[Track] = deque()
        self.queue_event = asyncio.Event()
        self.current: Optional[Track] = None
        self.next_event = asyncio.Event()
        self.loop_task: Optional[asyncio.Task] = None
        self.voice: Optional[discord.VoiceClient] = None
        self.ffmpeg_exe: str = FFMPEG_EXE
        self._stopped = False

        # Single sticky panel
        self.panel_channel_id: Optional[int] = None
        self.panel_message_id: Optional[int] = None
        self._panel_lock: asyncio.Lock = asyncio.Lock()

        # Timing + history (bounded)
        self._start_time_utc: Optional[datetime] = None
        self._history: list[Track] = []
        self._history_index: int = -1  # points into _history; -1 means no history yet
        self.repeat_mode: str = "off"   # off | one | all
        self._navigating_history: bool = False
        self._seq_counter: int = 0

        # Live UI updates
        self._ui_task: Optional[asyncio.Task] = None

        # Temp cookies file (when using YTDLP_COOKIES_B64)
        self._temp_cookie_path: Optional[Path] = None

        # Bump tracking
        self._last_panel_bump_monotonic: float = 0.0
        # Idle tracking (voice disconnect after inactivity)
        self._idle_started_mono: Optional[float] = None
        self._last_activity_mono: float = time.monotonic()
        self._idle_task: Optional[asyncio.Task] = None

    # ----- Cookies lifecycle for this player -----
    def _ensure_temp_cookie_file(self):
        if self._temp_cookie_path is None and YTDLP_COOKIES_B64:
            try:
                self._temp_cookie_path = write_temp_cookie_file_from_b64(YTDLP_COOKIES_B64)
                log.info("Created temp cookies file %s", self._temp_cookie_path)
            except Exception:
                log.exception("Failed to create temp cookies file from YTDLP_COOKIES_B64")

    def _cleanup_temp_cookie_file(self):
        if self._temp_cookie_path and self._temp_cookie_path.exists():
            try:
                self._temp_cookie_path.unlink(missing_ok=True)
                log.info("Deleted temp cookies file %s", self._temp_cookie_path)
            except Exception:
                log.debug("Failed to delete temp cookies file %s", self._temp_cookie_path)
        self._temp_cookie_path = None

    def ytdl_opts(self) -> Dict[str, Any]:
        opts = dict(BASE_YTDL_OPTS)
        if YTDLP_COOKIES_B64 and not YTDLP_COOKIES_FROM_BROWSER and not YTDLP_COOKIES_FILE:
            self._ensure_temp_cookie_file()
            if self._temp_cookie_path:
                opts["cookiefile"] = str(self._temp_cookie_path)
        return opts

    # ----- Idle + listeners -----
    def _has_humans(self) -> bool:
        if not self.voice or not self.voice.channel:
            return False
        members = getattr(self.voice.channel, "members", []) or []
        return any((not getattr(m, "bot", False)) for m in members)

    async def _safe_disconnect(self, reason: str):
        log.info("Disconnecting voice in guild %s: %s", self.guild.id, reason)
        try:
            if self.voice and self.voice.is_connected():
                await self.voice.disconnect(force=True)
        except Exception:
            log.debug("Voice disconnect failed: %s", reason, exc_info=True)
        self.voice = None
        self.current = None
        self.queue.clear()
        self._idle_started_mono = None
        self._last_activity_mono = time.monotonic()
        if DELETE_PANEL_ON_IDLE:
            await self.delete_panel()
        else:
            await self.post_or_update_panel()

    def _queue_empty(self) -> bool:
        return len(self.queue) == 0

    def _cancel_idle_timer(self):
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None
        self._idle_started_mono = None

    async def _start_idle_timer_if_needed(self):
        if not (self.voice and self.voice.is_connected()):
            return
        if self._has_humans() is False:
            self._cancel_idle_timer()
            await self._safe_disconnect("no humans")
            return
        if self.voice.is_playing() or self.voice.is_paused():
            self._cancel_idle_timer()
            return
        if not self._queue_empty():
            self._cancel_idle_timer()
            return
        if self._idle_task and not self._idle_task.done():
            return

        async def _idle_wait():
            self._idle_started_mono = time.monotonic()
            try:
                await asyncio.sleep(300)
                if not (self.voice and self.voice.is_connected()):
                    return
                if self.voice.is_playing() or self.voice.is_paused():
                    return
                if not self._queue_empty():
                    return
                if not self._has_humans():
                    await self._safe_disconnect("idle timeout (no humans)")
                    return
                await self._safe_disconnect("idle timeout (5m)")
            except asyncio.CancelledError:
                return
            finally:
                self._idle_task = None
                self._idle_started_mono = None

        self._idle_task = asyncio.create_task(_idle_wait())

    async def _on_track_start(self):
        self._cancel_idle_timer()
        self._last_activity_mono = time.monotonic()

    async def _on_track_end(self):
        await self._start_idle_timer_if_needed()

    async def _on_queue_updated(self):
        if not self._queue_empty():
            self._cancel_idle_timer()
        else:
            await self._start_idle_timer_if_needed()

    # ----- Core controls -----
    def ensure_loop(self):
        if self.loop_task is None or self.loop_task.done():
            self.loop_task = asyncio.create_task(self.player_loop())

    async def connect(self, channel: discord.VoiceChannel, *, timeout: int = 30):
        if self.voice and self.voice.is_connected():
            if self.voice.channel != channel:
                await self.voice.move_to(channel)
            return
        self.voice = await channel.connect(self_deaf=True, timeout=timeout, reconnect=True)

    async def enqueue(self, query: str, requester: Optional[discord.Member]) -> List[Track]:
        try:
            tracks = await create_tracks_from_query(query, ytdl_opts=self.ytdl_opts())
        except Exception as e:
            raise RuntimeError(str(e)) from e

        good: List[Track] = []
        for t in tracks:
            if not t or not t.stream_url:
                continue
            self._seq_counter += 1
            wrapped = Track(
                seq=self._seq_counter,
                title=t.title,
                stream_url=t.stream_url,
                webpage_url=t.webpage_url,
                duration=t.duration,
                thumbnail=t.thumbnail,
                requested_by=requester,
            )
            self.queue.append(wrapped)
            good.append(wrapped)

        if good:
            self.queue_event.set()
            self.ensure_loop()
            self._last_activity_mono = time.monotonic()
            await self._on_queue_updated()
        return good

    def _now(self): return datetime.now(timezone.utc)

    def estimated_position(self) -> Optional[int]:
        if not self._start_time_utc or not self.current or not self.current.duration:
            return None
        return int((self._now() - self._start_time_utc).total_seconds())

    def _progress_bar(self, width: int = 18) -> Optional[str]:
        if not self.current or not self.current.duration:
            return None
        pos = self.estimated_position() or 0
        ratio = min(max(pos / float(self.current.duration), 0.0), 1.0)
        filled = int(ratio * width)
        return f"{'▰'*filled}{'▱'*(width-filled)} {fmt_time(pos)}/{fmt_time(self.current.duration)}"

    def set_panel_channel(self, channel: discord.abc.Messageable, *, force: bool = False):
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            if force or self.panel_channel_id is None:
                self.panel_channel_id = channel.id

    def _alone_in_voice(self) -> bool:
        if not self.voice or not self.voice.channel:
            return False
        members = getattr(self.voice.channel, "members", None)
        if members is None:
            return False
        # Consider "alone" if only the bot remains.
        human_count = sum(1 for m in members if not getattr(m, "bot", False))
        return human_count == 0

    async def _resolve_panel_channel(self):
        if self.panel_channel_id:
            ch = self.bot.get_channel(self.panel_channel_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                return ch
        return None

    async def _cleanup_old_panels(self, ch: discord.abc.Messageable, keep_id: int):
        """Delete other panel messages from this bot in the same channel to keep a single panel visible."""
        if not self.bot.user:
            return
        try:
            async for m in ch.history(limit=30):
                if m.author.id != self.bot.user.id:
                    continue
                if m.id == keep_id:
                    continue
                if not m.embeds:
                    continue
                embed = m.embeds[0]
                if embed.author and embed.author.name == "DiscBot Music Console":
                    try:
                        await m.delete()
                    except Exception:
                        continue
        except Exception as e:
            log.debug("Panel cleanup skipped: %s", e)

    def _panel_embed(self) -> discord.Embed:
        palette = {"off": 0x5865F2, "one": 0xFEE75C, "all": 0x57F287}
        mode_badge = {"off": "🚫 Off", "one": "🔂 Single", "all": "🔁 Queue"}
        is_connected = bool(self.voice and self.voice.is_connected())
        is_playing = bool(self.voice and self.voice.is_playing())
        is_paused = bool(self.voice and self.voice.is_paused())
        has_track = self.current is not None

        if is_playing:
            status_line = "▶️ Streaming"
        elif is_paused and has_track:
            status_line = "⏸️ Waiting to resume"
        elif has_track:
            status_line = "📻 Standby"
        elif is_connected:
            status_line = "💤 Awaiting tracks"
        else:
            status_line = "💿 Use **/play** or **.play** to begin"

        embed = discord.Embed(
            title="DiscBot Music Console",
            description=status_line,
            color=palette.get(self.repeat_mode, 0x5865F2),
            timestamp=datetime.now(timezone.utc),
        )

        bot_logo = logo_embed_url()
        # Keep author clean (no icon) to avoid duplication and ensure clarity.

        playback_lines: list[str] = []
        if has_track and self.current:
            playback_lines.append(f"**[{self.current.title}]({self.current.webpage_url})**")
            prog = self._progress_bar()
            if prog:
                playback_lines.append(f"`{prog}`")
            if self.current.requested_by:
                playback_lines.append(f"Requested • {self.current.requested_by.mention}")
        elif has_track:
            playback_lines.append(f"Last track: **[{self.current.title}]({self.current.webpage_url})**")
        else:
            playback_lines.append("Queue a song with `/play` or `.play <search>`.")

        embed.add_field(name="Deck Feed", value="\n".join(playback_lines), inline=False)

        queue_len = len(self.queue)
        embed.add_field(name="Queue Depth", value=f"{queue_len} waiting", inline=True)
        embed.add_field(name="Repeat Mode", value=mode_badge.get(self.repeat_mode, self.repeat_mode), inline=True)

        upcoming = list(itertools.islice(self.queue, 5))
        if upcoming:
            lines = [f"`{idx+1:02d}` [{track.title}]({track.webpage_url})" for idx, track in enumerate(upcoming)]
            if queue_len > len(upcoming):
                lines.append(f"…and {queue_len - len(upcoming)} more in queue")
            embed.add_field(name="Up Next", value="\n".join(lines), inline=False)

        if bot_logo:
            embed.set_thumbnail(url=bot_logo)

        guild_icon = getattr(self.guild.icon, "url", None)
        footer = f"Panel • {self.guild.name} • Repeat {self.repeat_mode.upper()}"
        embed.set_footer(text=footer, icon_url=guild_icon or discord.Embed.Empty)
        return embed

    async def delete_panel(self):
        if not self.panel_message_id:
            return
        ch = await self._resolve_panel_channel()
        if not ch:
            return
        try:
            msg = await ch.fetch_message(self.panel_message_id)
            await msg.delete()
        except Exception:
            pass
        finally:
            self.panel_message_id = None

    async def post_or_update_panel(self):
        ch = await self._resolve_panel_channel()
        if not ch:
            return
        view = ControlView(self.bot, self)
        async with self._panel_lock:
            if self.panel_message_id:
                try:
                    msg = await ch.fetch_message(self.panel_message_id)
                    await msg.edit(embed=self._panel_embed(), view=view)
                    await self._cleanup_old_panels(ch, msg.id)
                    return
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    self.panel_message_id = None
            try:
                file = build_logo_file() if logo_requires_attachment() else None
                msg = await ch.send(embed=self._panel_embed(), view=view, file=file)
                self.panel_message_id = msg.id
                await self._cleanup_old_panels(ch, msg.id)
            except discord.Forbidden:
                log.warning("Missing perms to send panel in %s", ch)
            except discord.HTTPException as e:
                log.debug("Failed to send panel: %s", e)

    async def _bump_panel_if_needed(self, force: bool = False):
        if not self.panel_message_id:
            return
        if PANEL_BUMP_SECONDS <= 0 and not force:
            return
        ch = await self._resolve_panel_channel()
        if not ch:
            return

        try:
            last_id = ch.last_message_id
            should_bump = force or (last_id is not None and last_id != self.panel_message_id)
            now_mono = time.monotonic()
            if not force and (now_mono - self._last_panel_bump_monotonic) < max(PANEL_BUMP_SECONDS, 1):
                should_bump = False

            if should_bump:
                try:
                    old = await ch.fetch_message(self.panel_message_id)
                    await old.delete()
                except Exception:
                    pass
                file = build_logo_file() if logo_requires_attachment() else None
                msg = await ch.send(embed=self._panel_embed(), view=ControlView(self.bot, self), file=file)
                self.panel_message_id = msg.id
                self._last_panel_bump_monotonic = now_mono
                await self._cleanup_old_panels(ch, msg.id)
        except Exception as e:
            log.debug("Panel bump failed: %s", e)

    def _ffmpeg_source(self) -> discord.AudioSource:
        """
        High-quality Opus + stable timing. Removes '-re' and adds async resampler to keep speed in sync.
        """
        ch_bps = getattr(self.voice.channel, "bitrate", 128000) if self.voice else 128000

        cap_env = os.getenv("MUSIC_OPUS_BITRATE_MAX")
        cap_kbps = None
        if cap_env:
            try:
                cap_kbps = max(8, min(512, int(cap_env)))
            except Exception:
                cap_kbps = None

        kbps = ch_bps // 1000
        if cap_kbps is not None:
            kbps = min(kbps, cap_kbps)
        kbps = max(8, min(512, kbps))

        # Robust network flags; DO NOT throttle with -re
        before_opts = (
            "-nostdin "
            "-rw_timeout 30000000 "
            "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
            "-probesize 10000000 -analyzeduration 10000000"
        )

        # Keep timestamps and speed stable; resample deterministically to 48k stereo s16
        audio_filters = (
            "aresample=async=1:min_hard_comp=0.1:first_pts=0,"
            "aresample=48000,"
            "aformat=sample_fmts=s16:channel_layouts=stereo"
        )

        opus_quality_opts = (
            f"-vn -sn -dn -af {audio_filters} "
            "-application audio "
            "-vbr on "
            "-compression_level 10 "
            "-frame_duration 20"
        )

        return discord.FFmpegOpusAudio(
            self.current.stream_url,
            bitrate=kbps,
            before_options=before_opts,
            options=opus_quality_opts,
            executable=self.ffmpeg_exe,
        )

    # ----- History navigation & repeat -----
    def _update_history_for_track(self, track: Track):
        """
        Keep a bounded, ordered history with a cursor:
        - If we navigated, just move the cursor to the existing entry (append if missing).
        - If we are playing a new track while not navigating and we're not at the live edge,
          truncate any "forward" history first (browser-style).
        """
        if track is None:
            return

        existing_idx = next((i for i, t in enumerate(self._history) if t.seq == track.seq), None)

        if existing_idx is not None:
            self._history_index = existing_idx
        else:
            self._history.append(track)
            if len(self._history) > 5:
                overflow = len(self._history) - 5
                self._history = self._history[overflow:]
                self._history_index = max(0, len(self._history) - 1)
            else:
                self._history_index = len(self._history) - 1

        log.info(
            "History sync nav=%s idx=%s len=%s seq=%s title=%s queue_len=%s",
            self._navigating_history,
            self._history_index,
            len(self._history),
            getattr(track, "seq", None),
            getattr(track, "title", None),
            len(self.queue),
        )

    def play_previous(self):
        """
        Go back in history; if we're already at the first item, simply restart the current track.
        """
        if not self._history:
            return
        # If we're at the start of history, just restart the current track.
        if self._history_index <= 0:
            if self.current:
                self.queue.appendleft(self.current)
                log.info("History back restart seq=%s queue_len=%s", self.current.seq, len(self.queue))
                self.skip()
            return

        pointer = self._history_index - 1
        target = self._history[pointer]
        if self.current and not any(t.seq == self.current.seq for t in self.queue):
            # Keep the current track in the queue so forward navigation can return to it.
            self.queue.appendleft(self.current)
        self.queue.appendleft(target)
        old_idx = self._history_index
        self._history_index = pointer
        self._navigating_history = True
        log.info(
            "History back %s -> %s (seq=%s) queue_len=%s",
            old_idx,
            self._history_index,
            target.seq,
            len(self.queue),
        )
        self.skip()

    def can_go_previous(self) -> bool:
        # Allow restart when at the first item, or full back when deeper in history.
        return bool(self._history)

    def play_next_from_history(self):
        """Step forward in history if we had stepped back."""
        if not self._history or self._history_index == -1:
            return
        pointer = self._history_index
        if pointer >= len(self._history) - 1:
            return
        pointer += 1
        self._history_index = pointer
        self._navigating_history = True
        track = self._history[pointer]
        log.info(
            "History forward to idx=%s seq=%s queue_len=%s",
            self._history_index,
            track.seq,
            len(self.queue),
        )
        self.queue.appendleft(track)
        self.skip()
        if self._history_index >= len(self._history) - 1:
            self._history_index = len(self._history) - 1  # stay at live edge index

    async def _ui_updater(self):
        try:
            while self.voice and (self.voice.is_playing() or self.voice.is_paused()):
                await self.post_or_update_panel()
                await self._bump_panel_if_needed(force=False)
                await asyncio.sleep(max(0.5, PROGRESS_UPDATE_SECONDS))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("UI updater error: %s", e)

    async def player_loop(self):
        self._stopped = False
        while not self._stopped:
            # Disconnect if alone in voice
            if self.voice and self.voice.is_connected() and not self._has_humans():
                await self._safe_disconnect("no humans present")
                continue

            # Idle disconnect tracking (no queue, nothing playing/paused)
            if (
                self.voice
                and self.voice.is_connected()
                and not self.voice.is_playing()
                and not self.voice.is_paused()
                and not self.queue
            ):
                now_mono = time.monotonic()
                if self._idle_started_mono is None:
                    self._idle_started_mono = now_mono
                elif (now_mono - self._idle_started_mono) >= 300:
                    log.info(
                        "Idle timeout reached; disconnecting voice in guild %s (idle_seconds=%.1f)",
                        self.guild.id,
                        now_mono - self._idle_started_mono,
                    )
                    try:
                        await self.voice.disconnect(force=True)
                    except Exception:
                        log.debug("Voice disconnect failed during idle timeout", exc_info=True)
                    self.voice = None
                    self.current = None
                    self._idle_started_mono = None
                    if DELETE_PANEL_ON_IDLE:
                        await self.delete_panel()
                    else:
                        await self.post_or_update_panel()
                    continue
            else:
                self._idle_started_mono = None

            if not self.queue:
                self.queue_event.clear()
                try:
                    await asyncio.wait_for(self.queue_event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    if DELETE_PANEL_ON_IDLE and not self.current:
                        await self.delete_panel()
                    continue
            self.current = self.queue.popleft()
            if not self.voice or not self.voice.is_connected():
                self.current = None
                continue
            self._last_activity_mono = time.monotonic()

            # Record history before playback starts so navigation has the right cursor.
            self._update_history_for_track(self.current)

            # Reset the completion gate for this track so we don't immediately fast-forward
            self.next_event.clear()
            self._start_time_utc = datetime.now(timezone.utc)

            def after_play(err):
                if err:
                    log.exception("after_play error: %s", err)
                self.bot.loop.call_soon_threadsafe(self.next_event.set)

            try:
                await self._on_track_start()
                self.voice.play(self._ffmpeg_source(), after=after_play)

                await self.post_or_update_panel()
                await self._bump_panel_if_needed(force=True)

                if self._ui_task and not self._ui_task.done():
                    self._ui_task.cancel()
                self._ui_task = asyncio.create_task(self._ui_updater())
            except Exception as e:
                log.exception("Playback failed: %s", e)
                self.next_event.set()

            await self.next_event.wait()

            if self._ui_task and not self._ui_task.done():
                self._ui_task.cancel()
            self._ui_task = None
            self._start_time_utc = None

            self._cleanup_temp_cookie_file()
            self._navigating_history = False
            self._last_activity_mono = time.monotonic()

            # If nothing left to play, drop current reference so idle checks can trigger.
            if not self.queue and self.voice and not self.voice.is_playing() and not self.voice.is_paused():
                self.current = None

            await self._on_track_end()

            if self._stopped:
                if DELETE_PANEL_ON_IDLE:
                    await self.delete_panel()
                else:
                    self.current = None
                    await self.post_or_update_panel()
                continue

            # ===== Repeat logic =====
            if self.repeat_mode == "one" and self.current:
                self.queue.appendleft(self.current)

            if not self.queue and self.repeat_mode == "all" and self._history:
                self.queue.extend(self._history)

            # ===== Post-track UI =====
            if self.queue:
                await self.post_or_update_panel()
                continue

            await self.post_or_update_panel()
            await self._bump_panel_if_needed(force=True)

            if MUSIC_LAST_PLAYED_SECONDS > 0:
                try:
                    await asyncio.wait_for(asyncio.sleep(MUSIC_LAST_PLAYED_SECONDS), timeout=MUSIC_LAST_PLAYED_SECONDS + 1)
                except asyncio.TimeoutError:
                    pass

            if DELETE_PANEL_ON_IDLE:
                await self.delete_panel()
            else:
                self.current = None
                await self.post_or_update_panel()

    # ----- Controls (used by both buttons and prefix cmds)
    def pause(self):
        if self.voice and self.voice.is_playing():
            self.voice.pause()
            asyncio.create_task(self.post_or_update_panel())

    def resume(self):
        if self.voice and self.voice.is_paused():
            self.voice.resume()
            asyncio.create_task(self.post_or_update_panel())

    def stop(self):
        self._stopped = True
        if self.voice:
            self.voice.stop()
        self.queue.clear()
        asyncio.create_task(self.post_or_update_panel())
        self._cleanup_temp_cookie_file()

    def skip(self):
        if self.voice:
            self.voice.stop()
            asyncio.create_task(self.post_or_update_panel())


# =========================
# Cog
# =========================
class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not hasattr(self.bot, "_music_players"):
            self.bot._music_players = GLOBAL_MUSIC_PLAYERS
        self.players = self.bot._music_players
        self.bot.add_view(ControlView(self.bot))

    def get_player(self, guild: discord.Guild) -> GuildPlayer:
        p = self.players.get(guild.id)
        if not p:
            p = GuildPlayer(self.bot, guild)
            self.players[guild.id] = p
        return p

    # --- keep panel fresh on channel activity ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        p = self.players.get(message.guild.id)
        if not p or not p.panel_channel_id or message.channel.id != p.panel_channel_id:
            return
        await p.post_or_update_panel()
        await p._bump_panel_if_needed(force=False)

    # ---------- Slash Commands ----------
    @app_commands.command(name="play", description="Play from YouTube or Spotify link/search")
    async def slash_play(self, inter: discord.Interaction, query: str):
        await inter.response.defer(ephemeral=True)
        if not inter.guild or not inter.user.voice or not inter.user.voice.channel:
            return await inter.followup.send("Join a voice channel first.", ephemeral=True)
        p = self.get_player(inter.guild)
        await p.connect(inter.user.voice.channel)
        p.set_panel_channel(inter.channel, force=True)
        try:
            tracks = await p.enqueue(query, requester=inter.user)  # type: ignore
        except Exception as e:
            return await inter.followup.send(f"Couldn’t enqueue: `{e}`", ephemeral=True)

        if not tracks:
            return await inter.followup.send("No playable formats were found for that input.", ephemeral=True)
        await inter.followup.send(f"Enqueued **{len(tracks)}** track(s).", ephemeral=True)

    @app_commands.command(name="nowplaying", description="Show the current/last track panel")
    async def slash_nowplaying(self, inter: discord.Interaction):
        if not inter.guild:
            return
        p = self.get_player(inter.guild)
        await inter.response.send_message(embed=p._panel_embed(), ephemeral=True)

    @app_commands.command(name="repeat", description="Configure repeat mode with quick-select buttons")
    @app_commands.describe(mode="Optional quick set: off, one, or all")
    async def slash_repeat(self, inter: discord.Interaction, mode: Optional[str] = None):
        if not inter.guild:
            return await inter.response.send_message("Repeat controls only work in servers.", ephemeral=True)
        p = self.get_player(inter.guild)
        valid = {"off", "one", "all"}
        if mode:
            if mode not in valid:
                return await inter.response.send_message("Valid modes: off, one, all", ephemeral=True)
            p.repeat_mode = mode
            await inter.response.send_message(f"Repeat mode set to **{mode}**", ephemeral=True)
            await p.post_or_update_panel()
            return

        view = RepeatModeView(self, inter.guild, p.repeat_mode)
        await inter.response.send_message(
            "Select a repeat mode below to re-arm the deck.", view=view, ephemeral=True
        )

    panel = app_commands.Group(name="panel", description="Music panel controls")

    @panel.command(name="sethere", description="Attach the music panel to this channel")
    async def panel_sethere(self, inter: discord.Interaction):
        if not inter.guild or not isinstance(inter.channel, (discord.TextChannel, discord.Thread)):
            return await inter.response.send_message("Run this in a text channel.", ephemeral=True)
        p = self.get_player(inter.guild)
        p.set_panel_channel(inter.channel, force=True)
        await p.post_or_update_panel()
        await inter.response.send_message("✅ Panel attached here.", ephemeral=True)

async def setup(bot: commands.Bot):
    cog = Music(bot)
    await bot.add_cog(cog)
    if not any(cmd.name == "panel" for cmd in bot.tree.get_commands()):
        bot.tree.add_command(cog.panel)
    try:
        await bot.tree.sync()
    except Exception as e:
        log.debug("App command sync failed: %s", e)
