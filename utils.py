"""
utils.py – Utility functions and constants for smolplayer.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from typing import Any, List
from urllib.parse import unquote, urlparse

from constants import (
    APP_ID,
    APP_NAME,
    NOTIFICATION_ICON,
    NOTIFICATION_TIMEOUT_MS,
)

AUDIO_EXTS = {
    ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav",
    ".wma", ".alac", ".aiff", ".aif", ".ape", ".mp2", ".mp1",
    ".mka", ".dff", ".dsf", ".mpc", ".ra", ".spx", ".wv", ".tta", ".shn"
}

_COVER_TMPDIR = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "app", APP_ID
)
_COVER_TMPFILE = os.path.join(_COVER_TMPDIR, f"{APP_NAME}_cover")


def natural_sort_key(text: str) -> List[Any]:
    """
    Key function for natural sorting (e.g. track1, track2, track10).
    """
    return [
        int(c) if c.isdigit() else c.lower()
        for c in re.split(r"(\d+)", text)
    ]


def write_cover_art(data: bytes) -> str:
    """
    Write cover art image bytes to a temp file and return a file:// URI.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    else:
        ext = ".jpg"

    h = hashlib.md5(data).hexdigest()[:12]
    path = f"{_COVER_TMPFILE}_{h}{ext}"

    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)

    return f"file://{path}"


def normalize_file_path(uri_or_path: str) -> str:
    """
    Convert a file:// URI or raw path to an absolute local filesystem path.
    """
    if uri_or_path.startswith("file://"):
        path = unquote(urlparse(uri_or_path).path)
        if os.name == "nt" and path.startswith("/"):
            path = path[1:]
        return os.path.abspath(path)
    return os.path.abspath(uri_or_path)


def fmt_time(seconds: float) -> str:
    """Format seconds into M:SS or H:MM:SS string."""
    seconds = max(0.0, seconds)
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def send_notification(summary: str, body: str, icon: str = NOTIFICATION_ICON, urgency: int = 1) -> None:
    """
    Send a friendly desktop notification using smolplayer icon and normal urgency.
    urgency: 0=low, 1=normal, 2=critical
    """
    try:
        from config import get_config
        if not get_config().notifications_enabled:
            return
    except Exception:
        pass

    try:
        import dbus
        bus = dbus.SessionBus()
        notify_obj = bus.get_object("org.freedesktop.Notifications", "/org/freedesktop/Notifications")
        notify_iface = dbus.Interface(notify_obj, "org.freedesktop.Notifications")
        notify_iface.Notify(
            APP_NAME,
            0,
            icon,
            summary,
            body,
            [],
            {"urgency": dbus.Byte(urgency)},
            NOTIFICATION_TIMEOUT_MS,
        )
        return
    except Exception:
        pass

    try:
        urgency_str = "critical" if urgency == 2 else "low" if urgency == 0 else "normal"
        import subprocess
        subprocess.run(
            ["notify-send", "-u", urgency_str, "-a", APP_NAME, "-i", icon, summary, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        pass


def send_error_notification(summary: str, body: str) -> None:
    """Send error notification with app icon and normal urgency."""
    send_notification(summary, body, icon=NOTIFICATION_ICON, urgency=1)


class PowerInhibitor:
    """
    Manages system power and sleep inhibition over D-Bus when audio is playing.
    Inhibits sleep/idle when state is 'playing', and releases inhibition when 'paused' or 'stopped'.
    """

    def __init__(self, app_name: str = APP_NAME) -> None:
        self._app_name = app_name
        self._lock = threading.Lock()
        self._items: List[Any] = []

    def inhibit(self, reason: str = "Playing audio") -> None:
        with self._lock:
            if self._items:
                return

            try:
                import dbus
                bus = dbus.SessionBus()
                pm_obj = bus.get_object("org.freedesktop.PowerManagement", "/org/freedesktop/PowerManagement/Inhibit")
                pm_iface = dbus.Interface(pm_obj, "org.freedesktop.PowerManagement.Inhibit")
                cookie = pm_iface.Inhibit(self._app_name, reason)
                self._items.append(("pm", (bus, pm_iface, cookie)))
                return
            except Exception:
                pass

            try:
                import dbus
                sys_bus = dbus.SystemBus()
                logind_obj = sys_bus.get_object("org.freedesktop.login1", "/org/freedesktop/login1")
                logind_iface = dbus.Interface(logind_obj, "org.freedesktop.login1.Manager")
                fd = logind_iface.Inhibit("sleep", self._app_name, reason, "block")
                self._items.append(("logind", fd))
            except Exception:
                pass

    def uninhibit(self) -> None:
        with self._lock:
            for item_type, data in self._items:
                try:
                    if item_type in ("pm", "ss"):
                        bus, iface, cookie = data
                        try:
                            iface.UnInhibit(cookie)
                        except Exception:
                            iface.Uninhibit(cookie)
                    elif item_type == "logind":
                        fd = data
                        if hasattr(fd, "take"):
                            os.close(fd.take())
                        elif isinstance(fd, int):
                            os.close(fd)
                except Exception:
                    pass
            self._items.clear()

    def on_state_changed(self, new_state: str) -> None:
        if new_state == "playing":
            self.inhibit()
        else:
            self.uninhibit()

