"""
player.py – Audio playback engine for smolplayer.

Pipeline: file → ffmpeg (raw PCM decode, native rate/channels/format) → pump thread → pw-cat stdin
Volume is managed via PipeWire / pactl. Zero post-processing audio decoding.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

from utils import write_cover_art, send_error_notification
from i18n import _
from config import get_config
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

# Re-exports from probing and dac for modularity and backwards compatibility
from probing import (
    TrackInfo,
    probe_track,
    extract_cover,
    is_lossy_source,
    _FMT_MAP,
    _GST_FMT_MAP,
    _APLAY_FMT_MAP,
    _DEFAULT_FMT,
    LOSSY_EXTS,
    LOSSY_CODECS,
    LOSSLESS_CODECS,
    _probe_metadata,
    _probe_metadata_gstreamer,
    _normalize_peak,
    _find_tag_float,
)
from dac import (
    DacCapabilities,
    _get_pactl_output,
    parse_proc_asound_stream,
    parse_proc_asound_codec,
    _find_proc_asound_card_dir,
    get_dac_hardware_capabilities,
    check_dac_supports_track,
    get_system_sink_info,
    get_alsa_hw_device,
    _send_bit_perfect_failure_notification,
)

STATE_STOPPED = "stopped"
STATE_PAUSED  = "paused"
STATE_PLAYING = "playing"

CHUNK = PIPELINE_CHUNK_SIZE


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
        self._app_stop_event = threading.Event()

        self._is_setting_volume: bool = False
        self._last_vol_sync_time: float = 0.0
        self._pipeline_generation: int = 0
        self._replay_gain: int = 0
        self._replaygain_preamp: float = 0.0
        self._replaygain_default_preamp: float = 0.0
        self._replaygain_peaking: int = 0
        self._internal_resampler: int = 0
        self._gapless_playback: int = 0
        self._bit_perfect: int = 0
        self._lossy_bps: int = 0

        # Callbacks
        self.on_state_change:             Optional[Callable[[str], None]] = None
        self.on_position_update:          Optional[Callable[[float], None]] = None
        self.on_track_end:                Optional[Callable[[], None]] = None
        self.on_volume_change:            Optional[Callable[[float], None]] = None
        self.on_gapless_track_transition: Optional[Callable[[TrackInfo], None]] = None
        self.get_next_track:              Optional[Callable[[], Optional[Union[str, TrackInfo]]]] = None

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
    def replaygain_peaking(self) -> int:
        return self._replaygain_peaking

    @replaygain_peaking.setter
    def replaygain_peaking(self, mode: int) -> None:
        self._replaygain_peaking = max(0, min(2, int(mode)))

    @property
    def internal_resampler(self) -> int:
        return self._internal_resampler

    @internal_resampler.setter
    def internal_resampler(self, mode: int) -> None:
        self._internal_resampler = int(mode)

    @property
    def audiophile_mode(self) -> int:
        return self._internal_resampler

    @audiophile_mode.setter
    def audiophile_mode(self, mode: int) -> None:
        self._internal_resampler = int(mode)

    @property
    def gapless_playback(self) -> int:
        return self._gapless_playback

    @gapless_playback.setter
    def gapless_playback(self, mode: int) -> None:
        self._gapless_playback = int(mode)

    @property
    def bit_perfect(self) -> int:
        return self._bit_perfect

    @bit_perfect.setter
    def bit_perfect(self, mode: int) -> None:
        self._bit_perfect = int(mode)

    @property
    def lossy_bps(self) -> int:
        return self._lossy_bps

    @lossy_bps.setter
    def lossy_bps(self, mode: int) -> None:
        self._lossy_bps = int(mode)

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
            while not self._app_stop_event.is_set():
                proc = None
                try:
                    proc = subprocess.Popen(
                        ["pactl", "subscribe"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                    )
                    if proc.stdout:
                        for line in proc.stdout:
                            if self._app_stop_event.is_set():
                                break
                            if "sink-input" in line.lower():
                                self.sync_system_volume()
                except Exception:
                    time.sleep(2.0)
                finally:
                    if proc:
                        if proc.stdout:
                            try:
                                proc.stdout.close()
                            except Exception:
                                pass
                        try:
                            proc.terminate()
                            proc.wait(timeout=0.1)
                        except Exception:
                            pass

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

    def _prepare_track_info(self, info: TrackInfo) -> TrackInfo:
        """Ensure audio stream properties and duration are probed for pre-constructed TrackInfo."""
        base_info = probe_track(info.path)
        sample_rate = base_info.sample_rate
        channels = base_info.channels
        sample_fmt = base_info.sample_fmt
        ffmpeg_fmt = base_info.ffmpeg_fmt
        pwcat_fmt = base_info.pwcat_fmt
        bytes_per_sample = base_info.bytes_per_sample

        track_gain_db = info.track_gain_db if info.track_gain_db is not None else base_info.track_gain_db
        album_gain_db = info.album_gain_db if info.album_gain_db is not None else base_info.album_gain_db
        track_peak = info.track_peak if info.track_peak is not None else base_info.track_peak
        album_peak = info.album_peak if info.album_peak is not None else base_info.album_peak

        start_time = max(0.0, info.start_time)
        end_time = info.end_time
        if end_time is not None:
            duration = max(0.0, end_time - start_time)
        elif info.duration > 0.0:
            duration = info.duration
            end_time = start_time + duration
        else:
            duration = max(0.0, base_info.duration - start_time)
            end_time = base_info.duration

        title = info.title or base_info.title or os.path.basename(info.path)
        artist = info.artist or base_info.artist
        album = info.album or base_info.album

        return TrackInfo(
            path=info.path,
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            sample_rate=sample_rate,
            channels=channels,
            sample_fmt=sample_fmt,
            ffmpeg_fmt=ffmpeg_fmt,
            pwcat_fmt=pwcat_fmt,
            bytes_per_sample=bytes_per_sample,
            cover_url=info.cover_url or base_info.cover_url,
            track_gain_db=track_gain_db,
            album_gain_db=album_gain_db,
            track_peak=track_peak,
            album_peak=album_peak,
            start_time=start_time,
            end_time=end_time,
            track_number=info.track_number,
            disc_number=info.disc_number,
            cue_path=info.cue_path,
        )

    def load(self, target: str | TrackInfo) -> TrackInfo:
        """Probe track first, then stop pipeline cleanly."""
        if isinstance(target, TrackInfo):
            info = self._prepare_track_info(target)
        else:
            info = probe_track(target)

        target_fmt = (info.pwcat_fmt, info.sample_rate, info.channels)
        keep = (self._pwcat is not None and self._pwcat.poll() is None and getattr(self, "_pwcat_fmt", None) == target_fmt)
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
        keep = (self._pwcat is not None and self._pwcat.poll() is None and getattr(self, "_pwcat_fmt", None) == target_fmt)
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
        keep = (self._pwcat is not None and self._pwcat.poll() is None and getattr(self, "_pwcat_fmt", None) == target_fmt)
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

    def close(self) -> None:
        self._app_stop_event.set()
        self.stop()

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

    def _calc_replaygain_db(self, t: TrackInfo) -> Optional[float]:
        if self._replay_gain not in (1, 2):
            return None
        raw_gain = t.track_gain_db if self._replay_gain == 1 else t.album_gain_db
        has_rg = (raw_gain is not None)
        preamp = self._replaygain_preamp if has_rg else self._replaygain_default_preamp
        gain_db = (raw_gain + preamp) if has_rg else preamp

        # Mode 1: Apply appropriate gain (prevent clipping by lowering gain)
        if self._replaygain_peaking == 1:
            peak = t.track_peak if self._replay_gain == 1 else (t.album_peak or t.track_peak)
            if peak is not None and peak > 0.0:
                max_allowed_gain = -20.0 * math.log10(peak)
                if gain_db > max_allowed_gain:
                    gain_db = max_allowed_gain
            elif gain_db > 0.0:
                # If peak is not known, assume full-scale peak 1.0 (0 dBFS) so positive preamp does not clip
                gain_db = 0.0

        return gain_db

    def _replaygain_should_compress(self, t: TrackInfo, gain_db: Optional[float] = None) -> bool:
        """Check if dynamic range compression should be applied (peaking mode 2 and clips)."""
        if self._replay_gain not in (1, 2) or self._replaygain_peaking != 2:
            return False
        if gain_db is None:
            raw_gain = t.track_gain_db if self._replay_gain == 1 else t.album_gain_db
            has_rg = (raw_gain is not None)
            preamp = self._replaygain_preamp if has_rg else self._replaygain_default_preamp
            gain_db = (raw_gain + preamp) if has_rg else preamp

        if gain_db is None:
            return False

        peak = t.track_peak if self._replay_gain == 1 else (t.album_peak or t.track_peak)
        if peak is not None and peak > 0.0:
            output_peak = peak * (10.0 ** (gain_db / 20.0))
            return output_peak > 1.0
        # If peak is not known, any positive gain (e.g. from positive preamp or default preamp) can clip
        return gain_db > 0.0

    def _spawn_ffmpeg_proc(self, t: TrackInfo, seek_pos: float, is_bit_perfect: bool = False) -> Optional[subprocess.Popen]:
        if not shutil.which("ffmpeg"):
            return None
        effective_seek = t.start_time + seek_pos
        seek_args = ["-accurate_seek", "-ss", f"{effective_seek:.6f}"] if effective_seek > 0.0 else []
        if (t.end_time is not None or t.start_time > 0.0) and t.duration > 0.0:
            remaining = max(0.0, t.duration - seek_pos)
            seek_args.extend(["-t", f"{remaining:.6f}"])

        filters = []

        effective_fmt = t.ffmpeg_fmt
        if not is_bit_perfect:
            rg_db = self._calc_replaygain_db(t)
            if rg_db is not None:
                filters.append(f"volume={rg_db:.2f}dB:eval=once:precision=float")
                if self._replaygain_should_compress(t, rg_db):
                    filters.append("alimiter=limit=1.0:attack=5:release=50:asc=true")

            if self._internal_resampler == 1:
                sink_fmt, sink_rate = get_system_sink_info()
                effective_fmt = "f32le"
                filters.append(f"aresample={sink_rate}:resampler=soxr:precision=33:dither_method=triangular")

        rg_args = ["-af", ",".join(filters)] if filters else []

        cmd = [
            "ffmpeg", "-nostdin", "-v", "quiet",
            "-analyzeduration", FFPROBE_ANALYZEDURATION,
            "-probesize", FFPROBE_PROBESIZE,
            *seek_args,
            "-i", t.path,
            *rg_args,
            "-f", effective_fmt, "pipe:1",
        ]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=65536)
            if p.poll() is not None and p.returncode != 0:
                return None
            return p
        except Exception:
            return None

    def _spawn_gstreamer_proc(self, t: TrackInfo, seek_pos: float, is_bit_perfect: bool = False) -> Optional[subprocess.Popen]:
        if not shutil.which("gst-launch-1.0"):
            return None

        if is_bit_perfect:
            effective_fmt = t.pwcat_fmt
            effective_rate = t.sample_rate
            rg_filter = ""
        else:
            sink_fmt, sink_rate = get_system_sink_info()
            effective_fmt = "f32" if self._internal_resampler == 1 else t.pwcat_fmt
            effective_rate = sink_rate if self._internal_resampler == 1 else t.sample_rate

            rg_filter = ""
            rg_db = self._calc_replaygain_db(t)
            if rg_db is not None:
                vol = 10.0 ** (rg_db / 20.0)
                rg_filter += f"! audioconvert dithering=tpdf ! volume volume={vol:.6f} ! audioconvert dithering=tpdf "
                if self._replaygain_should_compress(t, rg_db):
                    rg_filter += "! audiodynamic characteristics=soft-knee mode=compressor threshold=0.0 ratio=20.0 "

            if self._internal_resampler == 1:
                rg_filter += f"! audioresample quality=10 ! capsfilter caps=audio/x-raw,rate={effective_rate} ! audioconvert dithering=tpdf "

        gst_fmt = _GST_FMT_MAP.get(effective_fmt.lower(), "S16LE")
        effective_seek = t.start_time + seek_pos
        has_stop = ((t.end_time is not None or t.start_time > 0.0) and t.duration > 0.0)
        stop_nanos = int((t.start_time + t.duration) * 1e9) if has_stop else -1
        start_nanos = int(effective_seek * 1e9)

        py_code = f'''
import sys, gi
try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    p = Gst.parse_launch('filesrc location="{t.path}" ! decodebin {rg_filter} ! audioconvert ! audio/x-raw,format={gst_fmt},layout=interleaved ! fdsink sync=false fd=1')
    p.set_state(Gst.State.PAUSED)
    p.get_state(Gst.CLOCK_TIME_NONE)
    if {has_stop}:
        p.seek(1.0, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE, Gst.SeekType.SET, int({effective_seek} * 1e9), Gst.SeekType.SET, int(({t.start_time} + {t.duration}) * 1e9))
    elif {effective_seek} > 0.0:
        p.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE, int({effective_seek} * 1e9))
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
                ]
                if rg_filter:
                    for part in rg_filter.strip().split():
                        if part != "!":
                            cmd_cli.extend(["!", part])
                cmd_cli.extend([
                    "!", "audioconvert",
                    "!", f"audio/x-raw,format={gst_fmt},layout=interleaved",
                    "!", "fdsink", "fd=1"
                ])
                return subprocess.Popen(cmd_cli, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=65536)
            return p
        except Exception:
            return None

    def _launch_pipeline(self, force_normal_mode: bool = False) -> bool:
        assert self._track is not None
        t = self._track
        use_direct_alsa = (self._bit_perfect == 1 and not force_normal_mode)

        if use_direct_alsa:
            alsa_dev = get_alsa_hw_device()
            supported, reason = check_dac_supports_track(alsa_dev, t)
            if not supported:
                _send_bit_perfect_failure_notification(os.path.basename(t.path), reason=reason)
                return self._launch_pipeline(force_normal_mode=True)

            aplay_fmt = _APLAY_FMT_MAP.get(t.pwcat_fmt.lower(), "S16_LE")
            target_fmt = ("alsa", alsa_dev, aplay_fmt, t.sample_rate, t.channels)
            effective_fmt = t.pwcat_fmt
            effective_rate = t.sample_rate
        else:
            sink_fmt, sink_rate = get_system_sink_info()
            effective_fmt = "f32" if self._internal_resampler == 1 else t.pwcat_fmt
            effective_rate = sink_rate if self._internal_resampler == 1 else t.sample_rate
            target_fmt = (effective_fmt, effective_rate, t.channels)

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
                bps = 4 if effective_fmt in ("f32", "f32le", "s32", "s32le", "s24") else (8 if effective_fmt in ("f64", "f64le") else (1 if effective_fmt == "u8" else 2))
                bytes_per_sec = effective_rate * t.channels * bps
                frame_size = t.channels * bps
                raw_silence = int(bytes_per_sec * PIPELINE_SILENCE_LEAD_IN)
                self._silence_bytes = (raw_silence // frame_size) * frame_size
            else:
                self._silence_bytes = 0

        # Spawn decoder (primary method with automatic fallback to secondary)
        cfg = get_config()
        primary_ffmpeg = (cfg.decode_method == 0)

        proc = None
        if primary_ffmpeg:
            proc = self._spawn_ffmpeg_proc(t, self._seek_position, is_bit_perfect=use_direct_alsa)
            if proc is None:
                proc = self._spawn_gstreamer_proc(t, self._seek_position, is_bit_perfect=use_direct_alsa)
        else:
            proc = self._spawn_gstreamer_proc(t, self._seek_position, is_bit_perfect=use_direct_alsa)
            if proc is None:
                proc = self._spawn_ffmpeg_proc(t, self._seek_position, is_bit_perfect=use_direct_alsa)

        if proc is None:
            send_error_notification(
                _("{app_name} Error").format(app_name=APP_NAME),
                _("Audio decoding failed for {name}. Neither FFmpeg nor GStreamer could process format.").format(name=os.path.basename(t.path))
            )
            self._ffmpeg = None
            return False

        self._ffmpeg = proc

        # If audio process cannot be reused, detach old stream cleanly and start new direct ALSA aplay or pw-cat
        if not can_reuse:
            if self._pwcat is not None:
                self._stop_pipeline(keep_pwcat=False)

            if use_direct_alsa:
                if not shutil.which("aplay"):
                    if self._ffmpeg:
                        self._ffmpeg.kill()
                        self._ffmpeg = None
                    _send_bit_perfect_failure_notification(os.path.basename(t.path), reason=_("aplay command not found"))
                    return self._launch_pipeline(force_normal_mode=True)

                alsa_cmd = [
                    "aplay", "-q",
                    "--disable-resample",
                    "--disable-format",
                    "--disable-softvol",
                    "-t", "raw",
                    "-f", aplay_fmt,
                    "-r", str(t.sample_rate),
                    "-c", str(t.channels),
                    "-D", alsa_dev,
                    "-",
                ]
                try:
                    self._pwcat = subprocess.Popen(
                        alsa_cmd, stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=65536,
                    )
                    self._pwcat_fmt = target_fmt
                    time.sleep(0.05)
                    if self._pwcat.poll() is not None and self._pwcat.returncode != 0:
                        err_msg = ""
                        if self._pwcat.stderr:
                            err_bytes = self._pwcat.stderr.read()
                            err_msg = err_bytes.decode("utf-8", errors="replace").strip()
                        raise RuntimeError(err_msg or f"aplay exited with return code {self._pwcat.returncode}")
                except Exception as exc:
                    if self._ffmpeg:
                        self._ffmpeg.kill()
                        self._ffmpeg = None
                    self._pwcat = None
                    self._pwcat_fmt = None
                    _send_bit_perfect_failure_notification(os.path.basename(t.path), reason=str(exc))
                    return self._launch_pipeline(force_normal_mode=True)
            else:
                pwcat_cmd = [
                    "pw-cat", "--playback", "--raw",
                    "--format", effective_fmt,
                    "--rate", str(effective_rate),
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
                    time.sleep(0.05)
                    if self._pwcat.poll() is not None and self._pwcat.returncode != 0:
                        raise RuntimeError("pw-cat exited with return code " + str(self._pwcat.returncode))
                except Exception as exc:
                    if self._ffmpeg:
                        self._ffmpeg.kill()
                        self._ffmpeg = None
                    self._pwcat = None
                    self._pwcat_fmt = None
                    send_error_notification(APP_NAME, _("Could not start pw-cat: {exc}").format(exc=exc))
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
                    # Natural EOF reached on current decoder
                    if self._gapless_playback == 1 and self.get_next_track and not (self._bit_perfect == 1):
                        next_target = self.get_next_track()
                        if next_target:
                            next_track = next_target if isinstance(next_target, TrackInfo) else probe_track(next_target)

                            is_compatible = False
                            if self._internal_resampler == 1:
                                is_compatible = True
                            elif getattr(self, "_pwcat_fmt", None) is not None:
                                current_fmt, current_rate, current_ch = self._pwcat_fmt
                                is_compatible = (
                                    next_track.sample_rate == current_rate
                                    and next_track.pwcat_fmt == current_fmt
                                    and next_track.channels == current_ch
                                )

                            if is_compatible:
                                try:
                                    ffmpeg_out.close()
                                except OSError:
                                    pass

                                cfg = get_config()
                                next_proc = None
                                if cfg.decode_method == 0:
                                    next_proc = self._spawn_ffmpeg_proc(next_track, 0.0)
                                    if next_proc is None:
                                        next_proc = self._spawn_gstreamer_proc(next_track, 0.0)
                                else:
                                    next_proc = self._spawn_gstreamer_proc(next_track, 0.0)
                                    if next_proc is None:
                                        next_proc = self._spawn_ffmpeg_proc(next_track, 0.0)

                                if next_proc and next_proc.stdout:
                                    with self._lock:
                                        if stop_evt.is_set() or gen != self._pipeline_generation:
                                            next_proc.kill()
                                            break
                                        self._track = next_track
                                        self._ffmpeg = next_proc
                                        self._play_start_pos = 0.0
                                        self._play_start_time = time.monotonic()
                                        self._bytes_written = 0
                                        self._consecutive_failures = 0

                                    ffmpeg_proc = next_proc
                                    ffmpeg_out = next_proc.stdout
                                    if self.on_gapless_track_transition:
                                        self.on_gapless_track_transition(next_track)
                                    continue
                    break  # Natural EOF and no gapless continuation

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
        except (BrokenPipeError, OSError, ValueError) as exc:
            with self._lock:
                is_current = (self._pipeline_generation == gen) and not stop_evt.is_set()
                was_bit_perfect = (self._bit_perfect == 1)
                track_name = os.path.basename(self._track.path) if self._track else ""

            if is_current and was_bit_perfect and self._bytes_written < 131072:
                _send_bit_perfect_failure_notification(track_name, reason=str(exc) if str(exc) else None)
                self._launch_pipeline(force_normal_mode=True)
                return
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
