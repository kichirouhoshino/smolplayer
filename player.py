"""
player.py – Audio engine for smolplayer.

Pipeline: file → ffmpeg (raw PCM decode, native rate/channels/format) → pump thread → pw-cat stdin
Volume is managed via PipeWire / pactl. Zero post-processing audio decoding.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from utils import write_cover_art, send_error_notification
from i18n import _
from constants import (
    APP_NAME,
    APP_ID,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_CHANNELS,
    DEFAULT_SAMPLE_FMT,
    DEFAULT_FFMPEG_FMT,
    FFPROBE_ANALYZEDURATION,
    FFPROBE_PROBESIZE,
    PIPELINE_SILENCE_LEAD_IN,
    PIPELINE_CHUNK_SIZE,
    PWCAT_DRAIN_BYTES,
    PWCAT_NODE_NAME,
    PWCAT_NODE_DESCRIPTION,
    PWCAT_LATENCY,
    VOLUME_SYNC_INTERVAL,
    VOLUME_SINK_RETRY_SECS,
    MAX_CONSECUTIVE_FAILURES,
)

STATE_STOPPED = "stopped"
STATE_PAUSED  = "paused"
STATE_PLAYING = "playing"

CHUNK = PIPELINE_CHUNK_SIZE

_FMT_MAP: dict[str, tuple[str, str, int]] = {
    "u8":    ("u8",    "u8",  1),
    "s16":   ("s16le", "s16", 2),
    "s16le": ("s16le", "s16", 2),
    "s16p":  ("s16le", "s16", 2),
    "s24":   ("s32le", "s32", 4),
    "s24le": ("s32le", "s32", 4),
    "s24p":  ("s32le", "s32", 4),
    "s32":   ("s32le", "s32", 4),
    "s32le": ("s32le", "s32", 4),
    "s32p":  ("s32le", "s32", 4),
    "flt":   ("f32le", "f32", 4),
    "fltle": ("f32le", "f32", 4),
    "fltp":  ("f32le", "f32", 4),
    "f32le": ("f32le", "f32", 4),
    "dbl":   ("f64le", "f64", 8),
    "dblle": ("f64le", "f64", 8),
    "dblp":  ("f64le", "f64", 8),
    "f64le": ("f64le", "f64", 8),
}
_DEFAULT_FMT: tuple[str, str, int] = (DEFAULT_FFMPEG_FMT, DEFAULT_SAMPLE_FMT, 2)


@dataclass
class TrackInfo:
    path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    sample_rate: int = 44100
    channels: int = 2
    sample_fmt: str = "s16"
    ffmpeg_fmt: str = "s16le"
    pwcat_fmt: str = "s16"
    bytes_per_sample: int = 2
    cover_url: Optional[str] = None
    track_gain_db: Optional[float] = None
    album_gain_db: Optional[float] = None


def extract_cover(path: str) -> Optional[bytes]:
    """Extract embedded cover art via ffmpeg."""
    if path.startswith(("http://", "https://")):
        return None
    try:
        res = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", path,
             "-map", "0:v:0", "-vframes", "1", "-f", "image2", "pipe:1"],
            capture_output=True, timeout=10,
        )
        return res.stdout if res.returncode == 0 and res.stdout else None
    except Exception:
        return None


import shutil
from config import get_config

_GST_FMT_MAP: dict[str, str] = {
    "s16": "S16LE",
    "s16le": "S16LE",
    "s24": "S32LE",
    "s24le": "S32LE",
    "s32": "S32LE",
    "s32le": "S32LE",
    "f32": "F32LE",
    "f32le": "F32LE",
    "f64": "F64LE",
    "f64le": "F64LE",
    "u8": "U8",
}


def _probe_metadata_gstreamer(path: str) -> Optional[dict]:
    """Probe audio metadata using GStreamer Discoverer."""
    try:
        import gi
        gi.require_version('Gst', '1.0')
        gi.require_version('GstPbutils', '1.0')
        from gi.repository import Gst, GstPbutils

        if not Gst.is_initialized():
            Gst.init(None)

        disc = GstPbutils.Discoverer.new(5 * Gst.SECOND)
        uri = path if path.startswith("file://") else f"file://{os.path.abspath(path)}"
        info = disc.discover_uri(uri)

        duration = info.get_duration() / Gst.SECOND if info.get_duration() else 0.0
        streams = info.get_audio_streams()
        if not streams:
            return None
        astream = streams[0]

        sr = astream.get_sample_rate() or DEFAULT_SAMPLE_RATE
        ch = astream.get_channels() or DEFAULT_CHANNELS
        depth = astream.get_depth() or 16

        if depth > 24 or depth == 32:
            raw_fmt = "s32"
        elif depth > 16 or depth == 24:
            raw_fmt = "s24"
        else:
            raw_fmt = "s16"

        ffmpeg_fmt, pwcat_fmt, bps = _FMT_MAP.get(raw_fmt, _DEFAULT_FMT)

        tags = info.get_tags()
        title = None
        artist = ""
        album = ""
        track_gain_db = None
        album_gain_db = None

        if tags:
            res, val = tags.get_string("title")
            if res:
                title = val
            res, val = tags.get_string("artist")
            if res:
                artist = val
            res, val = tags.get_string("album")
            if res:
                album = val

            for tag_name in ("replaygain-track-gain", "replaygain_track_gain", "r128-track-gain"):
                res, val = tags.get_string(tag_name)
                if res and val:
                    m = re.search(r"([-+]?\d+(?:\.\d+)?)", str(val))
                    if m:
                        track_gain_db = float(m.group(1))
                        break

            for tag_name in ("replaygain-album-gain", "replaygain_album_gain", "r128-album-gain"):
                res, val = tags.get_string(tag_name)
                if res and val:
                    m = re.search(r"([-+]?\d+(?:\.\d+)?)", str(val))
                    if m:
                        album_gain_db = float(m.group(1))
                        break

        return {
            "title": title,
            "artist": artist,
            "album": album,
            "duration": duration,
            "sample_rate": sr,
            "channels": ch,
            "sample_fmt": raw_fmt,
            "ffmpeg_fmt": ffmpeg_fmt,
            "pwcat_fmt": pwcat_fmt,
            "bytes_per_sample": bps,
            "track_gain_db": track_gain_db,
            "album_gain_db": album_gain_db,
        }
    except Exception:
        return None


def probe_track(path: str) -> TrackInfo:
    """
    Probe *path* for metadata using primary decoder method with automatic fallback.
    Returns TrackInfo immediately for instant audio playback start.
    Cover art is loaded asynchronously via fetch_cover_async().
    """
    cfg = get_config()
    _basename = os.path.splitext(os.path.basename(path))[0]

    meta = None
    if cfg.decode_method == 0:
        meta = _probe_metadata(path)
        if meta is None:
            meta = _probe_metadata_gstreamer(path)
    else:
        meta = _probe_metadata_gstreamer(path)
        if meta is None:
            meta = _probe_metadata(path)

    if meta is None:
        send_error_notification(
            _("{app_name} Error").format(app_name=APP_NAME),
            _("Unable to read audio metadata for {name} with FFmpeg or GStreamer.").format(name=os.path.basename(path))
        )
        return TrackInfo(path=path, title=_basename)

    return TrackInfo(
        path=path,
        title=meta.get("title") or _basename,
        artist=meta.get("artist", ""),
        album=meta.get("album", ""),
        duration=meta.get("duration", 0.0),
        sample_rate=meta.get("sample_rate", DEFAULT_SAMPLE_RATE),
        channels=meta.get("channels", DEFAULT_CHANNELS),
        sample_fmt=meta.get("sample_fmt", DEFAULT_SAMPLE_FMT),
        ffmpeg_fmt=meta.get("ffmpeg_fmt", DEFAULT_FFMPEG_FMT),
        pwcat_fmt=meta.get("pwcat_fmt", DEFAULT_SAMPLE_FMT),
        bytes_per_sample=meta.get("bytes_per_sample", 2),
        cover_url=None,
        track_gain_db=meta.get("track_gain_db"),
        album_gain_db=meta.get("album_gain_db"),
    )


def _probe_metadata(path: str) -> Optional[dict]:
    """Run ffprobe with optimized probe size and return a flat dict of track properties."""
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "quiet",
             "-analyzeduration", FFPROBE_ANALYZEDURATION,
             "-probesize", FFPROBE_PROBESIZE,
             "-print_format", "json",
             "-show_format", "-show_streams", "-select_streams", "a:0", path],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode != 0:
            return None
        data = json.loads(res.stdout)
    except Exception:
        return None

    fmt     = data.get("format", {})
    astream = (data.get("streams") or [{}])[0]

    # Merge container + stream tags; stream tags win
    tags = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
    tags.update({k.lower(): v for k, v in astream.get("tags", {}).items()})

    track_gain_db: Optional[float] = None
    album_gain_db: Optional[float] = None
    for k, v in tags.items():
        if "replaygain_track_gain" in k or "r128_track_gain" in k:
            m = re.search(r"([-+]?\d+(?:\.\d+)?)", str(v))
            if m:
                track_gain_db = float(m.group(1))
        elif "replaygain_album_gain" in k or "r128_album_gain" in k:
            m = re.search(r"([-+]?\d+(?:\.\d+)?)", str(v))
            if m:
                album_gain_db = float(m.group(1))

    raw_fmt = astream.get("sample_fmt", "s16")
    ffmpeg_fmt, pwcat_fmt, bps = _FMT_MAP.get(raw_fmt, _DEFAULT_FMT)

    return {
        "title":           tags.get("title"),
        "artist":          tags.get("artist") or tags.get("album_artist") or "",
        "album":           tags.get("album") or "",
        "duration":        float(fmt.get("duration") or 0.0),
        "sample_rate":     int(astream.get("sample_rate") or DEFAULT_SAMPLE_RATE),
        "channels":        int(astream.get("channels") or DEFAULT_CHANNELS),
        "sample_fmt":      raw_fmt,
        "ffmpeg_fmt":      ffmpeg_fmt,
        "pwcat_fmt":       pwcat_fmt,
        "bytes_per_sample": bps,
        "track_gain_db":   track_gain_db,
        "album_gain_db":   album_gain_db,
    }


class PlayerEngine:
    """Audio playback engine: ffmpeg → pump thread → pw-cat."""

    def __init__(self) -> None:
        self._lock  = threading.RLock()
        self._state = STATE_STOPPED
        self._track: Optional[TrackInfo] = None
        self._volume: float = 1.0

        self._seek_position: float = 0.0
        self._play_start_time: Optional[float] = None
        self._play_start_pos: float = 0.0
        self._bytes_written: int = 0
        self._consecutive_failures: int = 0
        self._silence_bytes: int = 0

        self._ffmpeg:      Optional[subprocess.Popen] = None
        self._pwcat:       Optional[subprocess.Popen] = None
        self._pump_thread: Optional[threading.Thread] = None
        self._stop_event   = threading.Event()

        self._is_setting_volume: bool = False
        self._last_vol_sync_time: float = 0.0
        self._pipeline_generation: int = 0
        self._replay_gain: int = 0
        self._replaygain_preamp: float = 0.0
        self._replaygain_default_preamp: float = 0.0

        # Callbacks
        self.on_state_change:    Optional[Callable[[str], None]] = None
        self.on_position_update: Optional[Callable[[float], None]] = None
        self.on_track_end:       Optional[Callable[[], None]] = None
        self.on_volume_change:   Optional[Callable[[float], None]] = None

        self._start_volume_listener()

    # ------------------------------------------------------------------ props

    @property
    def state(self) -> str:
        return self._state

    @property
    def track(self) -> Optional[TrackInfo]:
        return self._track

    @property
    def volume(self) -> float:
        self.sync_system_volume()
        return self._volume

    @property
    def replay_gain(self) -> int:
        return self._replay_gain

    @replay_gain.setter
    def replay_gain(self, mode: int) -> None:
        self._replay_gain = max(0, min(2, mode))

    @property
    def replaygain_preamp(self) -> float:
        return self._replaygain_preamp

    @replaygain_preamp.setter
    def replaygain_preamp(self, value: float) -> None:
        self._replaygain_preamp = float(value)

    @property
    def replaygain_default_preamp(self) -> float:
        return self._replaygain_default_preamp

    @replaygain_default_preamp.setter
    def replaygain_default_preamp(self, value: float) -> None:
        self._replaygain_default_preamp = float(value)

    @property
    def position(self) -> float:
        with self._lock:
            if not self._track or self._state != STATE_PLAYING or self._play_start_time is None:
                return self._seek_position
            elapsed = time.monotonic() - self._play_start_time
            return max(0.0, min(self._play_start_pos + elapsed, self._track.duration))

    def sync_system_volume(self) -> Optional[float]:
        """Query current PipeWire / PulseAudio sink-input volume and sync internal state."""
        now = time.monotonic()
        with self._lock:
            if self._is_setting_volume or (now - self._last_vol_sync_time < VOLUME_SYNC_INTERVAL):
                return self._volume
            self._last_vol_sync_time = now

        vol = self._query_system_volume()
        if vol is not None:
            changed = False
            with self._lock:
                if not self._is_setting_volume and abs(self._volume - vol) > 0.005:
                    self._volume = vol
                    changed = True
            if changed and self.on_volume_change:
                self.on_volume_change(vol)
            return vol
        return self._volume

    def _query_system_volume(self) -> Optional[float]:
        try:
            res = subprocess.run(
                ["pactl", "list", "sink-inputs"],
                capture_output=True, text=True, timeout=2,
            )
            if res.returncode == 0:
                blocks = res.stdout.split("Sink Input #")
                for b in blocks[1:]:
                    if any(x in b for x in (
                        f'node.name = "{PWCAT_NODE_NAME}"',
                        f'application.name = "{PWCAT_NODE_NAME}"',
                        f'media.name = "{PWCAT_NODE_NAME}"',
                    )):
                        m = re.search(r'Volume:\s*.*?\/\s*(\d+)%', b)
                        if m:
                            return int(m.group(1)) / 100.0
        except Exception:
            pass
        return None

    def _start_volume_listener(self) -> None:
        """Background thread listening for desktop volume panel changes in real time."""
        def _listener():
            while not self._stop_event.is_set():
                try:
                    proc = subprocess.Popen(
                        ["pactl", "subscribe"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                    )
                    for line in proc.stdout:
                        if self._stop_event.is_set():
                            break
                        if "sink-input" in line.lower():
                            self.sync_system_volume()
                except Exception:
                    time.sleep(2.0)

        threading.Thread(target=_listener, daemon=True, name=f"{APP_NAME}-vol-sync").start()

    # ---------------------------------------------------------------- helpers

    def _set_state(self, new_state: str) -> None:
        with self._lock:
            if self._state == new_state:
                return
            self._state = new_state
        if self.on_state_change:
            self.on_state_change(new_state)

    def _clamp_pos(self, pos: float) -> float:
        dur = self._track.duration if self._track else 0.0
        return max(0.0, min(pos, dur))

    # ----------------------------------------------------------------- public

    def load(self, path: str) -> TrackInfo:
        """Probe track first, then stop pipeline cleanly."""
        info = probe_track(path)
        target_fmt = (info.pwcat_fmt, info.sample_rate, info.channels)
        keep = (self._pwcat is not None and self._pwcat.poll() is None and self._pwcat_fmt == target_fmt)
        self._stop_pipeline(keep_pwcat=keep)
        with self._lock:
            self._track = info
            self._seek_position = 0.0
            self._play_start_pos = 0.0
            self._play_start_time = None
            self._bytes_written = 0
        self._set_state(STATE_STOPPED)
        return info

    def load_info(self, info: TrackInfo) -> None:
        """Load an already-probed TrackInfo."""
        target_fmt = (info.pwcat_fmt, info.sample_rate, info.channels)
        keep = (self._pwcat is not None and self._pwcat.poll() is None and self._pwcat_fmt == target_fmt)
        self._stop_pipeline(keep_pwcat=keep)
        with self._lock:
            self._track = info
            self._seek_position = 0.0
            self._play_start_pos = 0.0
            self._play_start_time = None
            self._bytes_written = 0
        self._set_state(STATE_STOPPED)

    def play(self, seek: Optional[float] = None) -> None:
        if not self._track:
            return
        target_fmt = (self._track.pwcat_fmt, self._track.sample_rate, self._track.channels)
        keep = (self._pwcat is not None and self._pwcat.poll() is None and self._pwcat_fmt == target_fmt)
        self._stop_pipeline(keep_pwcat=keep)
        with self._lock:
            if seek is not None:
                self._seek_position = self._clamp_pos(seek)
            self._bytes_written = 0
        if not self._launch_pipeline():
            self._set_state(STATE_STOPPED)
            return
        self._set_state(STATE_PLAYING)

    def pause(self) -> None:
        if self._state != STATE_PLAYING:
            return
        pos = self.position
        self._stop_pipeline()
        with self._lock:
            self._seek_position = pos
            self._bytes_written = 0
        self._set_state(STATE_PAUSED)

    def resume(self) -> None:
        if self._state != STATE_PAUSED:
            return
        if not self._launch_pipeline():
            self._set_state(STATE_STOPPED)
            return
        self._set_state(STATE_PLAYING)

    def toggle_pause(self) -> None:
        if self._state == STATE_PLAYING:
            self.pause()
        elif self._state == STATE_PAUSED:
            self.resume()

    def stop(self) -> None:
        self._stop_pipeline()
        with self._lock:
            self._seek_position = 0.0
            self._bytes_written = 0
        self._set_state(STATE_STOPPED)

    def seek(self, position: float) -> None:
        if not self._track:
            return
        prev_state = self._state
        if prev_state == STATE_PAUSED:
            # Pipeline is already stopped — just update the seek cursor.
            with self._lock:
                self._seek_position = self._clamp_pos(position)
                self._bytes_written = 0
            return
        self._stop_pipeline()
        with self._lock:
            self._seek_position = self._clamp_pos(position)
            self._bytes_written = 0
        if prev_state == STATE_PLAYING:
            if not self._launch_pipeline():
                self._set_state(STATE_STOPPED)
                return
            self._set_state(STATE_PLAYING)

    def fetch_cover_async(self, callback: Optional[Callable[[str], None]] = None) -> None:
        """Extract cover art asynchronously in background without blocking audio startup."""
        track = self._track
        if not track or not track.path or track.cover_url or track.path.startswith(("http://", "https://")):
            return

        def _worker():
            cover_bytes = extract_cover(track.path)
            if cover_bytes:
                cover_url = write_cover_art(cover_bytes)
                with self._lock:
                    if self._track and self._track.path == track.path:
                        self._track.cover_url = cover_url
                if callback:
                    callback(cover_url)

        threading.Thread(target=_worker, daemon=True, name=f"{APP_NAME}-cover").start()

    def set_volume(self, volume: float) -> None:
        """Store volume and apply immediately if playing."""
        val = max(0.0, min(1.0, volume))
        with self._lock:
            self._volume = val
            self._is_setting_volume = True
            if getattr(self, "_vol_thread_active", False):
                return
            self._vol_thread_active = True

        threading.Thread(
            target=self._apply_volume_worker, daemon=True, name=f"{APP_NAME}-vol"
        ).start()

    # --------------------------------------------------------------- internal

    def _apply_volume_worker(self) -> None:
        """Worker thread to debounce and apply volume via pactl."""
        try:
            while True:
                with self._lock:
                    vol = self._volume

                idx = self._find_smolplayer_sink_input()
                if idx is not None:
                    try:
                        subprocess.run(
                            ["pactl", "set-sink-input-volume", str(idx), f"{int(vol * 100)}%"],
                            capture_output=True, timeout=2,
                        )
                    except Exception:
                        pass

                with self._lock:
                    if self._volume == vol:
                        self._vol_thread_active = False
                        break
        finally:
            time.sleep(0.2)
            with self._lock:
                self._is_setting_volume = False

    def _find_smolplayer_sink_input(self) -> Optional[int]:
        """
        Find smolplayer's pactl sink-input by node.name.
        Retries for up to VOLUME_SINK_RETRY_SECS to cover the brief PipeWire registration delay.
        """
        deadline = time.monotonic() + VOLUME_SINK_RETRY_SECS
        while time.monotonic() < deadline:
            try:
                res = subprocess.run(
                    ["pactl", "list", "sink-inputs"],
                    capture_output=True, text=True, timeout=2,
                )
                if res.returncode == 0:
                    current_idx: Optional[int] = None
                    for line in res.stdout.splitlines():
                        s = line.strip()
                        if s.startswith("Sink Input #"):
                            current_idx = int(s.split("#")[1])
                        elif s == f'node.name = "{PWCAT_NODE_NAME}"' and current_idx is not None:
                            return current_idx
            except Exception:
                return None
            time.sleep(0.1)
        return None


    def _spawn_ffmpeg_proc(self, t: TrackInfo, seek_pos: float) -> Optional[subprocess.Popen]:
        if not shutil.which("ffmpeg"):
            return None
        seek_args = ["-accurate_seek", "-ss", f"{seek_pos:.6f}"] if seek_pos > 0.0 else []
        rg_args = []
        if self._replay_gain in (1, 2):
            raw_gain = t.track_gain_db if self._replay_gain == 1 else t.album_gain_db
            if raw_gain is not None:
                final_gain_db = raw_gain + self._replaygain_preamp
            else:
                final_gain_db = self._replaygain_default_preamp
            rg_args = ["-af", f"volume={final_gain_db:.2f}dB"]

        cmd = [
            "ffmpeg", "-nostdin", "-v", "quiet",
            "-analyzeduration", FFPROBE_ANALYZEDURATION,
            "-probesize", FFPROBE_PROBESIZE,
            *seek_args,
            "-i", t.path,
            *rg_args,
            "-f", t.ffmpeg_fmt, "pipe:1",
        ]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=65536)
            if p.poll() is not None and p.returncode != 0:
                return None
            return p
        except Exception:
            return None

    def _spawn_gstreamer_proc(self, t: TrackInfo, seek_pos: float) -> Optional[subprocess.Popen]:
        if not shutil.which("gst-launch-1.0"):
            return None
        gst_fmt = _GST_FMT_MAP.get(t.pwcat_fmt.lower(), "S16LE")

        rg_filter = ""
        if self._replay_gain in (1, 2):
            raw_gain = t.track_gain_db if self._replay_gain == 1 else t.album_gain_db
            if raw_gain is not None:
                final_gain_db = raw_gain + self._replaygain_preamp
            else:
                final_gain_db = self._replaygain_default_preamp
            vol = 10.0 ** (final_gain_db / 20.0)
            rg_filter = f"! audioconvert ! volume volume={vol:.6f}"

        py_code = f'''
import sys, gi
try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    p = Gst.parse_launch('filesrc location="{t.path}" ! decodebin {rg_filter} ! audioconvert ! audio/x-raw,format={gst_fmt},layout=interleaved ! fdsink sync=false fd=1')
    p.set_state(Gst.State.PAUSED)
    p.get_state(Gst.CLOCK_TIME_NONE)
    if {seek_pos} > 0.0:
        p.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE, int({seek_pos} * 1e9))
    p.set_state(Gst.State.PLAYING)
    bus = p.get_bus()
    bus.timed_pop_filtered(Gst.CLOCK_TIME_NONE, Gst.MessageType.ERROR | Gst.MessageType.EOS)
    p.set_state(Gst.State.NULL)
except Exception:
    sys.exit(1)
'''
        cmd = [sys.executable, "-c", py_code]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=65536)
            time.sleep(0.05)
            if p.poll() is not None and p.returncode != 0:
                if not shutil.which("gst-launch-1.0"):
                    return None
                cmd_cli = [
                    "gst-launch-1.0", "-q",
                    "filesrc", f"location={t.path}",
                    "!", "decodebin",
                    "!", "audioconvert",
                    "!", f"audio/x-raw,format={gst_fmt},layout=interleaved",
                    "!", "fdsink", "fd=1"
                ]
                return subprocess.Popen(cmd_cli, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=65536)
            return p
        except Exception:
            return None

    def _launch_pipeline(self) -> bool:
        assert self._track is not None
        t = self._track
        target_fmt = (t.pwcat_fmt, t.sample_rate, t.channels)

        can_reuse = (
            self._pwcat is not None
            and self._pwcat.poll() is None
            and getattr(self, "_pwcat_fmt", None) == target_fmt
        )

        with self._lock:
            self._pipeline_generation += 1
            gen = self._pipeline_generation
            stop_evt = threading.Event()
            self._stop_event = stop_evt

            self._play_start_pos = self._seek_position
            self._play_start_time = time.monotonic()
            if self._seek_position == 0.0 and not can_reuse:
                bytes_per_sec = t.sample_rate * t.channels * t.bytes_per_sample
                frame_size = t.channels * t.bytes_per_sample
                raw_silence = int(bytes_per_sec * PIPELINE_SILENCE_LEAD_IN)
                self._silence_bytes = (raw_silence // frame_size) * frame_size
            else:
                self._silence_bytes = 0

        # Spawn decoder (primary method with automatic fallback to secondary)
        cfg = get_config()
        primary_ffmpeg = (cfg.decode_method == 0)

        proc = None
        if primary_ffmpeg:
            proc = self._spawn_ffmpeg_proc(t, self._seek_position)
            if proc is None:
                proc = self._spawn_gstreamer_proc(t, self._seek_position)
        else:
            proc = self._spawn_gstreamer_proc(t, self._seek_position)
            if proc is None:
                proc = self._spawn_ffmpeg_proc(t, self._seek_position)

        if proc is None:
            send_error_notification(
                _("{app_name} Error").format(app_name=APP_NAME),
                _("Audio decoding failed for {name}. Neither FFmpeg nor GStreamer could process format.").format(name=os.path.basename(t.path))
            )
            self._ffmpeg = None
            return False

        self._ffmpeg = proc

        # If pwcat cannot be reused due to native format change, detach old stream cleanly and start new native pwcat
        if not can_reuse:
            if self._pwcat is not None:
                self._stop_pipeline(keep_pwcat=False)

            pwcat_cmd = [
                "pw-cat", "--playback", "--raw",
                "--format", t.pwcat_fmt,
                "--rate", str(t.sample_rate),
                "--channels", str(t.channels),
                "--media-type", "Audio",
                "--media-category", "Playback",
                "--media-role", "Music",
                "-P", f"node.name={PWCAT_NODE_NAME}",
                "-P", f"node.description={PWCAT_NODE_DESCRIPTION}",
                "-P", f"media.name={PWCAT_NODE_NAME}",
                "-P", f"app.name={PWCAT_NODE_NAME}",
                "--latency", PWCAT_LATENCY,
                "-",
            ]

            try:
                self._pwcat = subprocess.Popen(
                    pwcat_cmd, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=65536,
                )
                self._pwcat_fmt = target_fmt
            except Exception as exc:
                send_error_notification(APP_NAME, _("Could not start pw-cat: {exc}").format(exc=exc))
                if self._ffmpeg:
                    self._ffmpeg.kill()
                    self._ffmpeg = None
                self._pwcat = None
                self._pwcat_fmt = None
                return False

        ffmpeg_proc = self._ffmpeg
        pwcat_proc  = self._pwcat

        self._pump_thread = threading.Thread(
            target=self._pump_loop,
            args=(gen, stop_evt, ffmpeg_proc, pwcat_proc),
            daemon=True,
            name=f"{APP_NAME}-pump-{gen}",
        )
        self._pump_thread.start()
        return True

    def _pump_loop(
        self,
        gen: int,
        stop_evt: threading.Event,
        ffmpeg_proc: subprocess.Popen,
        pwcat_proc: subprocess.Popen,
    ) -> None:
        ffmpeg_out = ffmpeg_proc.stdout if ffmpeg_proc else None
        pwcat_in   = pwcat_proc.stdin   if pwcat_proc  else None
        if not ffmpeg_out or not pwcat_in:
            return

        last_pos_update = 0.0
        first_chunk = True
        try:
            while not stop_evt.is_set():
                chunk = ffmpeg_out.read(CHUNK)
                if not chunk:
                    break  # Natural EOF

                if first_chunk:
                    first_chunk = False
                    if self._silence_bytes > 0:
                        try:
                            pwcat_in.write(b"\x00" * self._silence_bytes)
                        except Exception:
                            pass

                pwcat_in.write(chunk)
                pwcat_in.flush()

                with self._lock:
                    if gen != self._pipeline_generation:
                        break
                    self._bytes_written += len(chunk)

                now = time.monotonic()
                if now - last_pos_update >= 0.25:
                    last_pos_update = now
                    if self.on_position_update and not stop_evt.is_set():
                        self.on_position_update(self.position)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            if ffmpeg_out:
                try:
                    ffmpeg_out.close()
                except OSError:
                    pass

            with self._lock:
                is_current = (self._pipeline_generation == gen) and not stop_evt.is_set()

            if is_current:
                if self._bytes_written == 0:
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        send_error_notification(
                            APP_NAME,
                            _("Playback stopped: audio system or decoder failed repeatedly."),
                        )
                        self._set_state(STATE_STOPPED)
                    else:
                        self._set_state(STATE_STOPPED)
                        callback = self.on_track_end
                        if callback:
                            callback()
                else:
                    self._consecutive_failures = 0
                    self._set_state(STATE_STOPPED)
                    callback = self.on_track_end
                    if callback:
                        callback()

    def _stop_pipeline(self, keep_pwcat: bool = False) -> None:
        self._stop_event.set()
        with self._lock:
            if self._state == STATE_PLAYING and self._play_start_time is not None:
                elapsed = time.monotonic() - self._play_start_time
                self._seek_position = max(0.0, min(self._play_start_pos + elapsed, self._track.duration if self._track else 0.0))
            self._play_start_time = None

        if (self._pump_thread
                and self._pump_thread is not threading.current_thread()
                and self._pump_thread.is_alive()):
            self._pump_thread.join(timeout=0.5)
        self._pump_thread = None

        if self._ffmpeg is not None:
            if self._ffmpeg.stdout:
                try:
                    self._ffmpeg.stdout.close()
                except Exception:
                    pass
            try:
                self._ffmpeg.kill()
                self._ffmpeg.wait(timeout=0.1)
            except Exception:
                pass
            self._ffmpeg = None

        if self._pwcat is not None:
            if keep_pwcat:
                return

            # Synchronously drain & close old pw-cat so the PipeWire stream node is removed from the graph.
            # This allows PipeWire to automatically re-negotiate and switch hardware DAC clock sample rates.
            if self._pwcat.stdin:
                try:
                    self._pwcat.stdin.write(b"\x00" * PWCAT_DRAIN_BYTES)
                    self._pwcat.stdin.flush()
                    self._pwcat.stdin.close()
                except Exception:
                    pass

            try:
                self._pwcat.wait(timeout=0.15)
            except Exception:
                try:
                    self._pwcat.terminate()
                    self._pwcat.wait(timeout=0.1)
                except Exception:
                    pass

            self._pwcat = None
            self._pwcat_fmt = None
