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

DEFAULT_CONFIG_TEXT = """# smolplayer configuration file
# Location: ~/.config/smolplayer/config.ini

[smolplayer]

# Enable ReplayGain
# 0 - Off
# 1 - Use Track Gain
# 2 - Use Album Gain
# Default is 0
replay_gain = 0

# Pre-amp gain for songs with ReplayGain info (in dB)
# Default is 0
replaygain_preamp = 0

# Pre-amp gain for songs without ReplayGain info (in dB)
# Default is 0
replaygain_default_preamp = 0

# Shuffle Algorithm
# 0 - Fisher-Yates (Modern Knuth Shuffle - Industry Standard, Fair Uniform Random)
# 1 - Smart Shuffle (Artist-spaced, prevents same artist/album back-to-back)
# 2 - True Random (Pure random selection with replacement)
# Default is 0
shuffle_algo = 0

# Sorting Method when shuffle is off
# 0 - By filename
# 1 - By title
# 2 - By disc and track number
# 3 - By artist & title
# Default is 0
sort_method = 0

# Set whether to play songs recursively on File Open Action
# 0 - False (single parent folder only)
# 1 - True (recursive subfolders)
# Default is 0
recurse_fileopen = 0

# Set whether to play songs recursively on "Open folder with" action
# 0 - False (top-level folder only)
# 1 - True (recursive subfolders)
# Default is 1
recurse_folderopen = 1

# Timeout: When smolplayer will close itself when music has not been
# playing for a certain period of time
# Set by minutes (0 = disabled)
# Default is 30
timeout = 30

# Set how much presence smolplayer has in certain areas.
# 0 - Tray and Notifications enabled
# 1 - Tray only
# 2 - Notifications only
# 3 - All off
# Default is 0
presence = 0

# Set whether to remember Shuffle and Repeat across app opens
# 0 - False (default)
# 1 - True
# Default is 0
remember_toggles = 0

# Set what to use as the audio decoding method
# 0 - ffmpeg (recommended)
# 1 - gstreamer
# If a format is not available on one method, it will fallback to the other
# Defaults to 0
decode_method = 0

# Audiophile Mode
# Uses FFmpeg's highest quality internal resampler (soxr with triangular dither and max precision)
# when formats don't match what's required by the output device.
# NOTE: Highly discouraged, as this adds significantly more CPU usage and PipeWire already does this task well!
# 0 - Disabled (recommended: zero processing, PipeWire handles resampling efficiently)
# 1 - Enabled (uses FFmpeg/GStreamer highest quality soxr/tpdf resampler + dither; higher CPU usage)
# Default is 0
audiophile_mode = 0
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
    "shuffle_algo": (
        "\n# Shuffle Algorithm\n"
        "# 0 - Fisher-Yates (Modern Knuth Shuffle - Industry Standard, Fair Uniform Random)\n"
        "# 1 - Smart Shuffle (Artist-spaced, prevents same artist/album back-to-back)\n"
        "# 2 - True Random (Pure random selection with replacement)\n"
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
        "\n# Set whether to remember Shuffle and Repeat across app opens\n"
        "# 0 - False (default)\n"
        "# 1 - True\n"
        "# Default is 0\n"
        "remember_toggles = 0\n"
    ),
    "decode_method": (
        "\n# Set what to use as the audio decoding method\n"
        "# 0 - ffmpeg (recommended)\n"
        "# 1 - gstreamer\n"
        "# If a format is not available on one method, it will fallback to the other\n"
        "# Defaults to 0\n"
        "decode_method = 0\n"
    ),
    "audiophile_mode": (
        "\n# Audiophile Mode\n"
        "# Uses FFmpeg's highest quality internal resampler (soxr with triangular dither and max precision)\n"
        "# when formats don't match what's required by the output device.\n"
        "# NOTE: Highly discouraged, as this adds significantly more CPU usage and PipeWire already does this task well!\n"
        "# 0 - Disabled (recommended: zero processing, PipeWire handles resampling efficiently)\n"
        "# 1 - Enabled (uses FFmpeg/GStreamer highest quality soxr/tpdf resampler + dither; higher CPU usage)\n"
        "# Default is 0\n"
        "audiophile_mode = 0\n"
    ),
}


@dataclass
class Config:
    replay_gain: int = 0
    replaygain_preamp: float = 0.0
    replaygain_default_preamp: float = 0.0
    shuffle_algo: int = 0
    sort_method: int = 0
    recurse_fileopen: int = 0
    recurse_folderopen: int = 1
    timeout: int = 30
    presence: int = 0
    remember_toggles: int = 0
    decode_method: int = 0
    audiophile_mode: int = 0

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
            cfg.shuffle_algo = sec.getint("shuffle_algo", 0)
            cfg.sort_method = sec.getint("sort_method", 0)
            cfg.recurse_fileopen = sec.getint("recurse_fileopen", 0)
            cfg.recurse_folderopen = sec.getint("recurse_folderopen", 1)
            cfg.timeout = sec.getint("timeout", 30)
            cfg.presence = sec.getint("presence", 0)
            cfg.remember_toggles = sec.getint("remember_toggles", 0)
            cfg.decode_method = sec.getint("decode_method", 0)
            cfg.audiophile_mode = sec.getint("audiophile_mode", 0)
    except Exception:
        pass

    return cfg


def load_toggles_state() -> tuple[bool, str]:
    """Load remembered (shuffle, loop_status) state."""
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return bool(data.get("shuffle", False)), str(data.get("loop_status", "None"))
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

