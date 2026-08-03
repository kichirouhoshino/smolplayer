"""
config.py – Configuration manager for smolplayer.

Loads and saves user settings from ~/.config/smolplayer/config.ini
"""

from __future__ import annotations

import configparser
import json
import os
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
"""


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

    @property
    def tray_enabled(self) -> bool:
        return self.presence in (0, 1)

    @property
    def notifications_enabled(self) -> bool:
        return self.presence in (0, 2)


def get_config() -> Config:
    """Load configuration from file, creating default config if absent."""
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
        if parser.has_section("smolplayer"):
            sec = parser["smolplayer"]
            cfg.replay_gain = sec.getint("replay_gain", 0)
            cfg.shuffle_algo = sec.getint("shuffle_algo", 0)
            cfg.sort_method = sec.getint("sort_method", 0)
            cfg.recurse_fileopen = sec.getint("recurse_fileopen", 0)
            cfg.recurse_folderopen = sec.getint("recurse_folderopen", 1)
            cfg.timeout = sec.getint("timeout", 30)
            cfg.presence = sec.getint("presence", 0)
            cfg.remember_toggles = sec.getint("remember_toggles", 0)
            cfg.decode_method = sec.getint("decode_method", 0)
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
