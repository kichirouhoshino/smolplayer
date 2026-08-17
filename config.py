"""
config.py – Configuration manager for smolplayer.

Loads and saves user settings from ~/.config/smolplayer/config.ini
"""

from __future__ import annotations

import configparser
import json
import os
import subprocess
from dataclasses import dataclass

_CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "smolplayer")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "config.ini")
_STATE_FILE = os.path.join(_CONFIG_DIR, "state.json")

DEFAULT_CONFIG_HEADER = """# smolplayer configuration file
# Location: ~/.config/smolplayer/config.ini

[smolplayer]
"""

_OPTION_BLOCKS: dict[str, str] = {
    "replay_gain": (
        "\n# Enable ReplayGain\n"
        "# 0 - Off\n"
        "# 1 - Use Track Gain\n"
        "# 2 - Use Album Gain\n"
        "# Default is 0\n"
        "replay_gain = 0\n"
    ),
    "shuffle_algo": (
        "\n# Shuffle Algorithm\n"
        "# 0 - Fisher-Yates\n"
        "# 1 - Smart Shuffle (Artist-spaced)\n"
        "# 2 - True Random\n"
        "# Default is 0\n"
        "shuffle_algo = 0\n"
    ),
    "sort_method": (
        "\n# Sorting Method when shuffle is off\n"
        "# 0 - By filename\n"
        "# 1 - By title\n"
        "# 2 - By disc and track number\n"
        "# 3 - By artist & title\n"
        "# Default is 0\n"
        "sort_method = 0\n"
    ),
    "recurse_fileopen": (
        "\n# Set whether to play songs recursively on File Open Action\n"
        "# 0 - False (single parent folder only)\n"
        "# 1 - True (recursive subfolders)\n"
        "# Default is 0\n"
        "recurse_fileopen = 0\n"
    ),
    "recurse_folderopen": (
        "\n# Set whether to play songs recursively on \"Open folder with\" action\n"
        "# 0 - False (top-level folder only)\n"
        "# 1 - True (recursive subfolders)\n"
        "# Default is 1\n"
        "recurse_folderopen = 1\n"
    ),
    "timeout": (
        "\n# Timeout: When smolplayer will close itself when music has not been\n"
        "# playing for a certain period of time\n"
        "# Set by minutes (0 = disabled)\n"
        "# Default is 30\n"
        "timeout = 30\n"
    ),
    "presence": (
        "\n# Set how much presence smolplayer has in certain areas.\n"
        "# 0 - Tray and Notifications enabled\n"
        "# 1 - Tray only\n"
        "# 2 - Notifications only\n"
        "# 3 - All off\n"
        "# Default is 0\n"
        "presence = 0\n"
    ),
    "remember_toggles": (
        "\n# Set whether to remember Shuffle and Repeat across play sessions\n"
        "# 0 - False (default)\n"
        "# 1 - True\n"
        "# Default is 0\n"
        "remember_toggles = 0\n"
    ),
    "decode_method": (
        "\n# Set what to use as the audio decoding method\n"
        "# 0 - ffmpeg (recommended)\n"
        "# 1 - gstreamer\n"
        "# If a format is not available on one method, it will fallback to the\n"
        "# other.\n"
        "# Defaults to 0\n"
        "decode_method = 0\n"
    ),
    "replaygain_preamp": (
        "\n# Pre-amp gain for songs with ReplayGain info (in dB)\n"
        "# Default is 0\n"
        "replaygain_preamp = 0\n"
    ),
    "replaygain_default_preamp": (
        "\n# Pre-amp gain for songs without ReplayGain info (in dB)\n"
        "# Default is 0\n"
        "replaygain_default_preamp = 0\n"
    ),
    "replaygain_peaking": (
        "\n# Set how will smolplayer handle peaks when ReplayGain is on\n"
        "# 0 - Disabled\n"
        "# 1 - Apply appropriate gain\n"
        "# 2 - Dynamic range compression (if it clips)\n"
        "# Default is 0\n"
        "replaygain_peaking = 0\n"
    ),
    "internal_resampler": (
        "\n# Set whether to use ffmpeg/gstreamer for resampling when format does not\n"
        "# match the output device. It is highly recommended to keep this DISABLED,\n"
        "# as PipeWire already does this efficiently\n"
        "# 0 - Disabled\n"
        "# 1 - Enabled\n"
        "# Default is 0\n"
        "internal_resampler = 0\n"
    ),
    "bit_perfect": (
        "\n# Enable Bit-Perfect Playback (Experimental)\n"
        "# When this fails, playback will fallback to normal mode until the next track.\n"
        "# Has a chance to break at times, especially when you attempt to play something\n"
        "# that your DAC does not support. Also you can't play audio from other apps at\n"
        "# the same time. Use with caution!\n"
        "# Default is 0\n"
        "bit_perfect = 0\n"
    ),
}

DEFAULT_CONFIG_TEXT = DEFAULT_CONFIG_HEADER + "".join(_OPTION_BLOCKS.values())


@dataclass
class Config:
    replay_gain: int = 0
    shuffle_algo: int = 0
    sort_method: int = 0
    recurse_fileopen: int = 0
    recurse_folderopen: int = 1
    timeout: int = 30
    presence: int = 0
    remember_toggles: int = 0
    decode_method: int = 0
    replaygain_preamp: float = 0.0
    replaygain_default_preamp: float = 0.0
    replaygain_peaking: int = 0
    internal_resampler: int = 0
    bit_perfect: int = 0

    @property
    def audiophile_mode(self) -> int:
        return self.internal_resampler

    @property
    def tray_enabled(self) -> bool:
        return self.presence in (0, 1)

    @property
    def notifications_enabled(self) -> bool:
        return self.presence in (0, 2)


def get_config() -> Config:
    """Load configuration from file, creating default config if absent or updating missing options."""
    cfg = Config()
    if not os.path.exists(_CONFIG_FILE):
        try:
            os.makedirs(_CONFIG_DIR, exist_ok=True)
            with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
                f.write(DEFAULT_CONFIG_TEXT)
        except OSError:
            pass
        return cfg

    parser = configparser.ConfigParser()
    try:
        parser.read(_CONFIG_FILE, encoding="utf-8")
        existing_keys = set(parser["smolplayer"].keys()) if parser.has_section("smolplayer") else set()

        missing_keys = [k for k in _OPTION_BLOCKS if k not in existing_keys]
        if missing_keys:
            try:
                with open(_CONFIG_FILE, "a", encoding="utf-8") as f:
                    for k in missing_keys:
                        f.write(_OPTION_BLOCKS[k])
                parser.read(_CONFIG_FILE, encoding="utf-8")
            except OSError:
                pass

        if parser.has_section("smolplayer"):
            sec = parser["smolplayer"]
            cfg.replay_gain = sec.getint("replay_gain", 0)
            cfg.replaygain_preamp = sec.getfloat("replaygain_preamp", 0.0)
            cfg.replaygain_default_preamp = sec.getfloat("replaygain_default_preamp", 0.0)
            cfg.replaygain_peaking = sec.getint("replaygain_peaking", 0)
            cfg.shuffle_algo = sec.getint("shuffle_algo", 0)
            cfg.sort_method = sec.getint("sort_method", 0)
            cfg.recurse_fileopen = sec.getint("recurse_fileopen", 0)
            cfg.recurse_folderopen = sec.getint("recurse_folderopen", 1)
            cfg.timeout = sec.getint("timeout", 30)
            cfg.presence = sec.getint("presence", 0)
            cfg.remember_toggles = sec.getint("remember_toggles", 0)
            cfg.decode_method = sec.getint("decode_method", 0)
            # Support internal_resampler with fallback to legacy audiophile_mode key
            if "internal_resampler" in sec:
                cfg.internal_resampler = sec.getint("internal_resampler", 0)
            else:
                cfg.internal_resampler = sec.getint("audiophile_mode", 0)
            cfg.bit_perfect = sec.getint("bit_perfect", 0)
    except Exception:
        pass

    return cfg


def normalize_loop_status(value: Any) -> str:
    """Normalize loop status across desktop environments and MPRIS clients."""
    if isinstance(value, bool):
        return "Playlist" if value else "None"
    if isinstance(value, (int, float)):
        return "Playlist" if value else "None"
    s = str(value).strip().lower()
    if s in ("none", "off", "false", "0", ""):
        return "None"
    if s in ("track", "single", "one"):
        return "Track"
    if s in ("playlist", "all", "true", "1", "repeat"):
        return "Playlist"
    return "None"


def load_toggles_state() -> tuple[bool, str]:
    """Load remembered (shuffle, loop_status) state."""
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                shuf = bool(data.get("shuffle", False))
                raw_loop = data.get("loop_status", "None")
                norm_loop = normalize_loop_status(raw_loop)
                return shuf, norm_loop
    except Exception:
        pass
    return False, "None"


def save_toggles_state(shuffle: bool, loop_status: str) -> None:
    """Save (shuffle, loop_status) state for next app launch."""
    try:
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"shuffle": shuffle, "loop_status": loop_status}, f)
    except Exception:
        pass


def get_config_file_path() -> str:
    """Return the path to the config file."""
    return _CONFIG_FILE


def open_config_file() -> None:
    """Ensure config file exists and open it with system default application via xdg-open."""
    get_config()
    cfg_file = get_config_file_path()
    try:
        subprocess.Popen(["xdg-open", cfg_file])
    except Exception:
        try:
            subprocess.Popen(["gio", "open", cfg_file])
        except Exception:
            pass

