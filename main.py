#!/usr/bin/env python3
"""
main.py – Entry point for smolplayer.

smolplayer is a GUI-less music player driven by MPRIS D-Bus interfaces.
"""

from __future__ import annotations

import os
import sys
import signal
import threading
from typing import Optional
from player import PlayerEngine
from playlist import PlaylistManager
from mpris import MprisService, forward_files_to_existing, quit_existing_instance
from utils import normalize_file_path, send_error_notification, send_notification, PowerInhibitor
from tray import TrayService

from config import get_config, open_config_file
from constants import APP_NAME, APP_VERSION
from i18n import _


class AutoCloseManager:
    def __init__(self, timeout_minutes: int, on_timeout_callback) -> None:
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._timeout_seconds = float(timeout_minutes * 60)
        self._on_timeout = on_timeout_callback

    def on_state_changed(self, new_state: str) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

            if new_state == "paused" and self._timeout_seconds > 0:
                self._timer = threading.Timer(self._timeout_seconds, self._trigger_timeout)
                self._timer.daemon = True
                self._timer.start()

    def _trigger_timeout(self) -> None:
        mins = int(self._timeout_seconds // 60)
        send_notification(
            APP_NAME,
            _("Closed after being paused for {mins} minutes to save system resources.").format(mins=mins)
        )
        if self._on_timeout:
            self._on_timeout()


class MainLoop:
    """GLib main loop wrapper using gi.repository if available, or libglib-2.0 via ctypes."""

    def __init__(self) -> None:
        self._gi_loop = None
        self._c_lib = None
        self._c_loop = None

        try:
            from gi.repository import GLib
            self._gi_loop = GLib.MainLoop()
        except ImportError:
            import ctypes
            self._c_lib = ctypes.CDLL("libglib-2.0.so.0")
            self._c_lib.g_main_loop_new.restype = ctypes.c_void_p
            self._c_lib.g_main_loop_new.argtypes = [ctypes.c_void_p, ctypes.c_bool]
            self._c_lib.g_main_loop_run.argtypes = [ctypes.c_void_p]
            self._c_lib.g_main_loop_quit.argtypes = [ctypes.c_void_p]
            self._c_loop = self._c_lib.g_main_loop_new(None, False)

    def run(self) -> None:
        if self._gi_loop is not None:
            self._gi_loop.run()
        elif self._c_loop is not None and self._c_lib is not None:
            self._c_lib.g_main_loop_run(self._c_loop)

    def quit(self) -> None:
        if self._gi_loop is not None:
            self._gi_loop.quit()
        elif self._c_loop is not None and self._c_lib is not None:
            self._c_lib.g_main_loop_quit(self._c_loop)


def main() -> None:

    args = sys.argv[1:]
    cfg = get_config()

    if "--version" in args or "-v" in args:
        print(f"{APP_NAME} {APP_VERSION}")
        sys.exit(0)

    if "--config" in args or "-c" in args:
        open_config_file()
        sys.exit(0)

    if "--close" in args or "--quit" in args or "-q" in args:
        quit_existing_instance()
        sys.exit(0)


    # 1. Check for single instance; forward files if already running
    if args and forward_files_to_existing(args):
        sys.exit(0)

    engine = PlayerEngine()
    engine.replay_gain = cfg.replay_gain
    engine.replaygain_preamp = cfg.replaygain_preamp
    engine.replaygain_default_preamp = cfg.replaygain_default_preamp
    engine.internal_resampler = cfg.internal_resampler
    engine.bit_perfect = cfg.bit_perfect
    playlist = PlaylistManager()
    power_inhibitor = PowerInhibitor()
    loop = MainLoop()

    def handle_quit() -> None:
        power_inhibitor.uninhibit()
        engine.close()
        loop.quit()
        sys.exit(0)

    autoclose = AutoCloseManager(timeout_minutes=cfg.timeout, on_timeout_callback=handle_quit)

    mpris = None
    tray = TrayService(engine, playlist, on_quit=handle_quit, on_open_config=open_config_file) if cfg.tray_enabled else None

    def handle_state_change(new_state: str) -> None:
        if mpris:
            mpris.notify_state(new_state)
        if tray:
            tray.notify_state(new_state)
        autoclose.on_state_changed(new_state)
        power_inhibitor.on_state_changed(new_state)

    def handle_volume_change(new_vol: float) -> None:
        if mpris:
            mpris.notify_volume(new_vol)

    def handle_toggles_change(shuffle: bool, loop_status: str) -> None:
        if mpris:
            mpris.notify_shuffle(shuffle)
            mpris.notify_loop(loop_status)

    engine.on_state_change = handle_state_change
    engine.on_volume_change = handle_volume_change
    playlist.on_toggles_changed = handle_toggles_change

    def play_track(track_path: Optional[str]) -> bool:
        if not track_path or not os.path.isfile(track_path):
            return False
        info = engine.load(track_path)
        engine.play()
        if mpris:
            mpris.notify_track(info, playlist.current_index)
        if tray:
            tray.notify_track(info)

        def _on_cover_done(url):
            if mpris:
                mpris.notify_track(engine.track, playlist.current_index)
            if tray:
                tray.notify_track(engine.track)

        engine.fetch_cover_async(callback=_on_cover_done)
        return True

    def handle_open_uri(uri: str) -> None:
        path = normalize_file_path(uri)
        if not os.path.exists(path):
            send_error_notification(_("{app_name} File Error").format(app_name=APP_NAME), _("File or folder does not exist: {name}").format(name=os.path.basename(path)))
            return

        current = playlist.load_file_or_folder(path)
        if not play_track(current):
            send_error_notification(_("{app_name} File Error").format(app_name=APP_NAME), _("Unable to find playable audio files for: {name}").format(name=os.path.basename(path)))

    def handle_track_end() -> None:
        attempts = 0
        max_attempts = len(playlist)

        while attempts < max_attempts:
            next_track = playlist.get_next(auto_advance=(attempts == 0))
            if not next_track:
                break
            attempts += 1
            if play_track(next_track):
                return

        engine.stop()
        if attempts > 0:
            send_notification(
                APP_NAME,
                _("Playback stopped: files or drive are no longer accessible."),
            )

    engine.on_track_end = handle_track_end

    # Handle signal interrupts
    def signal_handler(sig, frame):
        handle_quit()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 1. Start audio playback FIRST before registering D-Bus or showing notifications
    if args:
        handle_open_uri(args[0])
    else:
        send_notification(
            APP_NAME,
            _("{app_name} runs in the background! Open an audio file in your file manager to start music playback and control it using your desktop media widget.").format(app_name=APP_NAME),
        )

    # 2. Register MPRIS D-Bus service while audio is already playing
    mpris = MprisService(engine, playlist, on_quit=handle_quit, on_open_uri=handle_open_uri)
    if engine.track:
        mpris.notify_track(engine.track, playlist.current_index)
        if tray:
            tray.notify_track(engine.track)

    # Run GLib main loop (required for D-Bus signal delivery)
    loop.run()


if __name__ == "__main__":
    main()
