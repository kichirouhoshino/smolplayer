"""
probing.py – Audio track data models, codec maps, cover art extraction, and metadata probing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

from utils import write_cover_art, send_error_notification
from i18n import _
from config import get_config
from constants import (
    APP_NAME,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_CHANNELS,
    DEFAULT_SAMPLE_FMT,
    DEFAULT_FFMPEG_FMT,
    FFPROBE_ANALYZEDURATION,
    FFPROBE_PROBESIZE,
)

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

_APLAY_FMT_MAP: dict[str, str] = {
    "s16": "S16_LE",
    "s16le": "S16_LE",
    "s24": "S32_LE",
    "s24le": "S32_LE",
    "s32": "S32_LE",
    "s32le": "S32_LE",
    "f32": "FLOAT_LE",
}

LOSSY_EXTS: set[str] = {
    ".mp3", ".aac", ".ogg", ".opus", ".wma", ".mpc", ".spx", ".ra", ".ram", ".mp2", ".mp1",
}
LOSSY_CODECS: set[str] = {
    "mp3", "mp3float", "aac", "aac_latm", "vorbis", "opus", "wmav1", "wmav2", "wmavoice", "wmapro",
    "mpc", "mpc7", "mpc8", "musepack", "speex", "ac3", "eac3", "dca", "dts", "cook", "ra_144", "ra_288",
    "atrac1", "atrac3", "atrac3p", "atrac9", "twinvq", "nellymoser", "siren", "amrnb", "amrwb", "gsm",
    "mp2", "mp1",
}
LOSSLESS_CODECS: set[str] = {
    "flac", "alac", "wavpack", "ape", "monkeysaudio", "shorten", "tta", "tak", "mlp", "truehd",
}


def is_lossy_source(path: str, codec_name: Optional[str] = None, sample_fmt: Optional[str] = None) -> bool:
    """Determine whether an audio track is from a lossy source."""
    if codec_name:
        c = codec_name.lower()
        if c in LOSSY_CODECS:
            return True
        if c in LOSSLESS_CODECS or c.startswith("pcm_") or c.startswith("dsd_"):
            return False
    ext = os.path.splitext(path)[1].lower()
    if ext in LOSSY_EXTS:
        return True
    if ext in {".flac", ".ape", ".tta", ".shn", ".wav", ".aiff", ".aif", ".wv"}:
        return False
    if sample_fmt and sample_fmt.lower() in ("flt", "fltp", "dbl", "dblp"):
        return True
    return False


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
    track_peak: Optional[float] = None
    album_peak: Optional[float] = None
    start_time: float = 0.0
    end_time: Optional[float] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    cue_path: Optional[str] = None


def extract_cover(path: str) -> Optional[bytes]:
    """Extract embedded cover art via ffmpeg or fallback to folder artwork."""
    if path.startswith(("http://", "https://")):
        return None
    try:
        res = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", path,
             "-map", "0:v:0", "-vframes", "1", "-f", "image2", "pipe:1"],
            capture_output=True, timeout=10,
        )
        if res.returncode == 0 and res.stdout:
            return res.stdout
    except Exception:
        pass

    try:
        folder = os.path.dirname(path)
        if folder and os.path.isdir(folder):
            cover_names = (
                "cover.jpg", "cover.png", "cover.jpeg", "cover.webp",
                "folder.jpg", "folder.png", "folder.jpeg", "folder.webp",
                "front.jpg", "front.png", "front.jpeg", "front.webp",
                "album.jpg", "album.png", "album.jpeg", "album.webp",
                "artwork.jpg", "artwork.png", "artwork.jpeg", "artwork.webp",
            )
            with os.scandir(folder) as entries:
                existing = {e.name.lower(): e.path for e in entries if e.is_file()}
            for name in cover_names:
                if name in existing:
                    with open(existing[name], "rb") as f:
                        return f.read()
    except Exception:
        pass

    return None


def _normalize_peak(val: Optional[float], sample_fmt: str = "s16") -> Optional[float]:
    """Normalize peak value to 0.0-1.0 float range, handling legacy integer peak tags (8-bit, 16-bit, 24-bit, 32-bit)."""
    if val is None or val <= 0.0:
        return None
    # If peak was written as a raw integer sample value rather than a 0.0-1.0 ratio
    if val > 1.5:
        if sample_fmt == "u8" and val <= 256.0:
            return val / 256.0
        elif val <= 32768.0:
            return val / 32768.0
        elif val <= 65536.0:
            return val / 65536.0
        elif val <= 8388608.0:
            return val / 8388608.0
        elif val <= 2147483648.0:
            return val / 2147483648.0
    return val


def _find_tag_float(tag_dict: dict, primary_keys: tuple[str, ...], fallback_keys: tuple[str, ...] = ()) -> Optional[float]:
    """Find and parse floating point tag value from dictionary matching given keys."""
    for k, v in tag_dict.items():
        k_lower = str(k).lower()
        if any(pk.lower() in k_lower for pk in primary_keys):
            m = re.search(r"([-+]?\d+(?:\.\d+)?)", str(v))
            if m:
                return float(m.group(1))
    for k, v in tag_dict.items():
        k_lower = str(k).lower()
        if any(fk.lower() in k_lower for fk in fallback_keys):
            m = re.search(r"([-+]?\d+(?:\.\d+)?)", str(v))
            if m:
                return float(m.group(1))
    return None


def _get_active_config() -> Any:
    try:
        import player
        return getattr(player, "get_config", get_config)()
    except Exception:
        return get_config()


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

        cfg = _get_active_config()
        if cfg.lossy_bps == 1 and is_lossy_source(path, sample_fmt=raw_fmt):
            raw_fmt = "s16"
            ffmpeg_fmt, pwcat_fmt, bps = ("s16le", "s16", 2)
        else:
            ffmpeg_fmt, pwcat_fmt, bps = _FMT_MAP.get(raw_fmt, _DEFAULT_FMT)

        tags = info.get_tags()
        title = None
        artist = ""
        album = ""
        track_gain_db = None
        album_gain_db = None
        track_peak = None
        album_peak = None

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

            def _find_gst_tag(primary_tags: tuple[str, ...], fallback_tags: tuple[str, ...]) -> Optional[float]:
                for tag_name in primary_tags:
                    res, val = tags.get_string(tag_name)
                    if res and val:
                        m = re.search(r"([-+]?\d+(?:\.\d+)?)", str(val))
                        if m:
                            return float(m.group(1))
                for tag_name in fallback_tags:
                    res, val = tags.get_string(tag_name)
                    if res and val:
                        m = re.search(r"([-+]?\d+(?:\.\d+)?)", str(val))
                        if m:
                            return float(m.group(1))
                return None

            track_gain_db = _find_gst_tag(("replaygain-track-gain", "replaygain_track_gain"), ("r128-track-gain", "r128_track_gain"))
            album_gain_db = _find_gst_tag(("replaygain-album-gain", "replaygain_album_gain"), ("r128-album-gain", "r128_album_gain"))
            track_peak = _normalize_peak(_find_gst_tag(("replaygain-track-peak", "replaygain_track_peak"), ("r128-track-peak", "r128_track_peak")), raw_fmt)
            album_peak = _normalize_peak(_find_gst_tag(("replaygain-album-peak", "replaygain_album_peak"), ("r128-album-peak", "r128_album_peak")), raw_fmt)

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
            "track_peak": track_peak,
            "album_peak": album_peak,
        }
    except Exception:
        return None


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

    codec_name = astream.get("codec_name")
    raw_fmt = astream.get("sample_fmt", "s16")
    cfg = _get_active_config()
    if cfg.lossy_bps == 1 and is_lossy_source(path, codec_name=codec_name, sample_fmt=raw_fmt):
        ffmpeg_fmt, pwcat_fmt, bps = ("s16le", "s16", 2)
        raw_fmt = "s16"
    else:
        ffmpeg_fmt, pwcat_fmt, bps = _FMT_MAP.get(raw_fmt, _DEFAULT_FMT)

    track_gain_db = _find_tag_float(tags, ("replaygain_track_gain", "replaygain-track-gain"), ("r128_track_gain", "r128-track-gain"))
    album_gain_db = _find_tag_float(tags, ("replaygain_album_gain", "replaygain-album-gain"), ("r128_album_gain", "r128-album-gain"))
    track_peak = _normalize_peak(_find_tag_float(tags, ("replaygain_track_peak", "replaygain-track-peak"), ("r128_track_peak", "r128-track-peak")), raw_fmt)
    album_peak = _normalize_peak(_find_tag_float(tags, ("replaygain_album_peak", "replaygain-album-peak"), ("r128_album_peak", "r128-album-peak")), raw_fmt)

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
        "track_peak":      track_peak,
        "album_peak":      album_peak,
    }


def probe_track(path: str) -> TrackInfo:
    """
    Probe *path* for metadata using primary decoder method with automatic fallback.
    Returns TrackInfo immediately for instant audio playback start.
    Cover art is loaded asynchronously via fetch_cover_async().
    """
    cfg = _get_active_config()
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
        track_peak=meta.get("track_peak"),
        album_peak=meta.get("album_peak"),
    )
