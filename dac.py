"""
dac.py – Hardware DAC capability detection, ALSA hardware descriptors, and PipeWire sink inspection.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from i18n import _
from utils import send_error_notification
from constants import APP_NAME
from probing import TrackInfo, _APLAY_FMT_MAP


@dataclass
class DacCapabilities:
    formats: set[str] = field(default_factory=set)
    sample_rates: set[int] = field(default_factory=set)
    min_rate: Optional[int] = None
    max_rate: Optional[int] = None
    max_channels: int = 2


def _get_pactl_output() -> tuple[Optional[str], Optional[str]]:
    """Helper to query pactl info for default sink name and list sinks output."""
    try:
        default_sink = None
        res_info = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=2)
        if res_info.returncode == 0:
            for line in res_info.stdout.splitlines():
                if line.startswith("Default Sink:"):
                    default_sink = line.split("Default Sink:")[1].strip()
                    break

        res_sinks = subprocess.run(["pactl", "list", "sinks"], capture_output=True, text=True, timeout=2)
        sinks_txt = res_sinks.stdout if res_sinks.returncode == 0 else None
        return default_sink, sinks_txt
    except Exception:
        return None, None


def get_system_sink_info() -> tuple[str, int]:
    """Query current active default PipeWire sink sample format ('s16' or 's32') and sample rate."""
    try:
        import player
        pactl_fn = getattr(player, "_get_pactl_output", _get_pactl_output)
    except Exception:
        pactl_fn = _get_pactl_output
    default_sink, sinks_txt = pactl_fn()
    if sinks_txt:
        current_name = None
        for line in sinks_txt.splitlines():
            s = line.strip()
            if s.startswith("Name:"):
                current_name = s.split("Name:")[1].strip()
            elif "Sample Specification:" in s:
                m = re.search(r"(\w+)\s+\d+ch\s+(\d+)Hz", s)
                if m:
                    fmt_str = m.group(1).lower()
                    rate = int(m.group(2))
                    pw_fmt = "s16" if "s16" in fmt_str else "s32"
                    if default_sink and current_name == default_sink:
                        return pw_fmt, rate
                    elif not default_sink:
                        return pw_fmt, rate
    return "s16", 48000


def get_alsa_hw_device() -> str:
    """Detect active default ALSA hardware device (e.g. 'plughw:1,0') or fallback."""
    try:
        import player
        pactl_fn = getattr(player, "_get_pactl_output", _get_pactl_output)
    except Exception:
        pactl_fn = _get_pactl_output
    default_sink, sinks_txt = pactl_fn()
    if sinks_txt:
        current_card = None
        current_dev = "0"
        is_default = False
        for line in sinks_txt.splitlines():
            s = line.strip()
            if s.startswith("Name:"):
                name = s.split("Name:")[1].strip()
                is_default = (default_sink and name == default_sink)
            elif "alsa.card =" in s:
                current_card = s.split("alsa.card =")[1].strip().strip('"')
            elif "alsa.device =" in s:
                current_dev = s.split("alsa.device =")[1].strip().strip('"')
                if is_default and current_card is not None:
                    return f"plughw:{current_card},{current_dev}"
    return "plughw:0,0"


def parse_proc_asound_stream(content: str) -> DacCapabilities:
    """Parse ALSA USB Audio Class stream descriptor (/proc/asound/cardX/stream0)."""
    caps = DacCapabilities()
    in_playback = False
    for line in content.splitlines():
        line_s = line.strip()
        if line_s.startswith("Playback:"):
            in_playback = True
            continue
        elif line_s.startswith("Capture:"):
            in_playback = False
            continue

        if not in_playback:
            continue

        m_fmt = re.search(r"Format:\s*(\S+)", line_s)
        if m_fmt:
            raw_f = m_fmt.group(1).strip()
            if raw_f.upper() != "SPECIAL":
                caps.formats.add(raw_f.upper())
            elif "DSD" in line_s.upper():
                caps.formats.add("DSD")

        m_ch = re.search(r"Channels:\s*(\d+)", line_s)
        if m_ch:
            caps.max_channels = max(caps.max_channels, int(m_ch.group(1)))

        if line_s.startswith("Rates:"):
            rates_body = line_s.split("Rates:", 1)[1]
            m_range = re.search(r"(\d{4,7})\s*-\s*(\d{4,7})", rates_body)
            if m_range:
                r_min, r_max = int(m_range.group(1)), int(m_range.group(2))
                caps.min_rate = min(caps.min_rate, r_min) if caps.min_rate else r_min
                caps.max_rate = max(caps.max_rate, r_max) if caps.max_rate else r_max
            for num_match in re.finditer(r"\b(\d{4,7})\b", rates_body):
                r = int(num_match.group(1))
                if 8000 <= r <= 1536000:
                    caps.sample_rates.add(r)

    if caps.sample_rates:
        if caps.min_rate is None:
            caps.min_rate = min(caps.sample_rates)
        if caps.max_rate is None:
            caps.max_rate = max(caps.sample_rates)
    return caps


def parse_proc_asound_codec(content: str) -> DacCapabilities:
    """Parse HDA Intel/Realtek codec descriptor (/proc/asound/cardX/codec#*)."""
    caps = DacCapabilities()
    has_pcm = False
    for line in content.splitlines():
        line_s = line.strip()
        if "rates [" in line_s:
            parts = line_s.split("]:", 1)[-1]
            for num in re.findall(r"\b(\d{4,7})\b", parts):
                r = int(num)
                if 8000 <= r <= 1536000:
                    caps.sample_rates.add(r)
        elif "bits [" in line_s:
            parts = line_s.split("]:", 1)[-1]
            bit_nums = [int(b) for b in re.findall(r"\b(\d+)\b", parts)]
            if 16 in bit_nums:
                caps.formats.add("S16_LE")
            if any(b in bit_nums for b in (20, 24, 32)):
                caps.formats.update({"S32_LE", "S24_3LE", "S24_LE"})
        elif "formats [" in line_s and "PCM" in line_s:
            has_pcm = True

    if has_pcm and not caps.formats:
        caps.formats.update({"S16_LE", "S32_LE"})

    if caps.sample_rates:
        caps.min_rate = min(caps.sample_rates)
        caps.max_rate = max(caps.sample_rates)
    return caps


def _find_proc_asound_card_dir(card_spec: str) -> Optional[str]:
    """Resolve card identifier (number or name) to /proc/asound/cardX path."""
    if not card_spec:
        return None
    direct = f"/proc/asound/card{card_spec}"
    if os.path.isdir(direct):
        return direct
    by_name = f"/proc/asound/{card_spec}"
    if os.path.isdir(by_name):
        return by_name
    cards_file = "/proc/asound/cards"
    if os.path.isfile(cards_file):
        try:
            with open(cards_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = re.search(r"^\s*(\d+)\s+\[([^\]]+)\]", line)
                    if m:
                        idx, name = m.group(1), m.group(2).strip()
                        if card_spec in (idx, name):
                            target = f"/proc/asound/card{idx}"
                            if os.path.isdir(target):
                                return target
        except OSError:
            pass
    return None


def get_dac_hardware_capabilities(alsa_dev: str) -> Optional[DacCapabilities]:
    """Query hardware capabilities of the ALSA sound card/DAC from /proc/asound."""
    m = re.search(r"(?:hw:|plughw:)(?:CARD=)?([^,]+)", alsa_dev)
    if not m:
        return None
    card_spec = m.group(1).strip()
    card_dir = _find_proc_asound_card_dir(card_spec)
    if not card_dir or not os.path.isdir(card_dir):
        return None

    # Check USB stream descriptors
    try:
        for entry in os.listdir(card_dir):
            if entry.startswith("stream"):
                stream_path = os.path.join(card_dir, entry)
                if os.path.isfile(stream_path):
                    with open(stream_path, "r", encoding="utf-8", errors="ignore") as f:
                        caps = parse_proc_asound_stream(f.read())
                        if caps.formats or caps.sample_rates:
                            return caps
    except OSError:
        pass

    # Check HDA / codec descriptors
    try:
        for entry in os.listdir(card_dir):
            if entry.startswith("codec#"):
                codec_path = os.path.join(card_dir, entry)
                if os.path.isfile(codec_path):
                    with open(codec_path, "r", encoding="utf-8", errors="ignore") as f:
                        caps = parse_proc_asound_codec(f.read())
                        if caps.formats or caps.sample_rates:
                            return caps
    except OSError:
        pass

    return None


def check_dac_supports_track(alsa_dev: str, track: TrackInfo) -> tuple[bool, Optional[str]]:
    """
    Validate whether the DAC hardware natively supports the track's sample rate, format, and channels.
    Returns (True, None) if supported, or (False, error_reason) if unsupported.
    """
    try:
        import player
        get_caps = getattr(player, "get_dac_hardware_capabilities", get_dac_hardware_capabilities)
    except Exception:
        get_caps = get_dac_hardware_capabilities

    caps = get_caps(alsa_dev)
    if caps is None:
        return True, None

    # 1. Channels check
    if track.channels > caps.max_channels:
        return False, _("Track has {ch} channels, but DAC hardware only supports up to {max_ch} channels").format(
            ch=track.channels, max_ch=caps.max_channels
        )

    # 2. Sample rate check
    if caps.sample_rates or (caps.min_rate and caps.max_rate):
        if caps.sample_rates and track.sample_rate not in caps.sample_rates:
            if caps.min_rate and caps.max_rate and (caps.min_rate <= track.sample_rate <= caps.max_rate):
                pass  # Supported continuous range
            else:
                supp_str = ", ".join(f"{r}Hz" for r in sorted(caps.sample_rates))
                return False, _("Sample rate {rate}Hz is not supported by DAC (supported: {supp})").format(
                    rate=track.sample_rate, supp=supp_str
                )
        elif caps.min_rate and caps.max_rate and not (caps.min_rate <= track.sample_rate <= caps.max_rate):
            return False, _("Sample rate {rate}Hz is outside DAC hardware range ({min_r}-{max_r}Hz)").format(
                rate=track.sample_rate, min_r=caps.min_rate, max_r=caps.max_rate
            )

    # 3. Format check
    aplay_fmt = _APLAY_FMT_MAP.get(track.pwcat_fmt.lower())
    if aplay_fmt and caps.formats:
        int_fmts = {"S16_LE", "S24_3LE", "S24_LE", "S32_LE"}
        if aplay_fmt == "FLOAT_LE" and "FLOAT_LE" not in caps.formats:
            return False, _("32-bit floating point audio is not supported by DAC in direct ALSA mode")
        if aplay_fmt in int_fmts and not (caps.formats & int_fmts):
            return False, _("Integer PCM format {fmt} is not supported by DAC").format(fmt=aplay_fmt)
        if aplay_fmt not in int_fmts and aplay_fmt not in caps.formats:
            return False, _("Sample format {fmt} is not supported by DAC").format(fmt=aplay_fmt)

    return True, None


def _send_bit_perfect_failure_notification(track_name: str, reason: Optional[str] = None) -> None:
    try:
        import player
        send_err = getattr(player, "send_error_notification", send_error_notification)
    except Exception:
        send_err = send_error_notification

    if reason:
        msg = _("Bit-perfect playback failed for {name}:\n{reason}\nFalling back to normal playback mode.").format(
            name=track_name, reason=reason
        )
    else:
        msg = _("Bit-perfect playback failed for {name}. Falling back to normal playback mode.").format(name=track_name)
    send_err(
        _("{app_name} Bit-Perfect Warning").format(app_name=APP_NAME),
        msg,
    )
