"""
test_smolplayer.py – Unit and integration test suite for smolplayer.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from utils import (
    AUDIO_EXTS,
    PLAYLIST_EXTS,
    CUE_EXTS,
    is_audio_file,
    is_playlist_file,
    is_cue_file,
    fmt_time,
    natural_sort_key,
    normalize_file_path,
    write_cover_art,
)
from playlist import (
    PlaylistManager,
    scan_audio_files_recursive,
    scan_audio_files_single_folder,
    parse_cue,
    parse_m3u,
    parse_pls,
    parse_xspf,
    parse_playlist_file,
    parse_cue_time,
)
from player import (
    PlayerEngine, TrackInfo, STATE_PLAYING, STATE_PAUSED, STATE_STOPPED,
    get_system_sink_info, _probe_metadata, extract_cover, is_lossy_source,
    DacCapabilities, parse_proc_asound_stream, parse_proc_asound_codec,
    get_dac_hardware_capabilities, check_dac_supports_track,
)
from tray import TrayService
from mpris import MprisService, _track_id
from config import Config, get_config, open_config_file, get_config_file_path
from constants import APP_NAME, TRACK_OBJ_PATH

import probing
import dac
import parsers

_ORIGINAL_XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME")
_TEST_TEMP_CONFIG_DIR: Optional[tempfile.TemporaryDirectory] = None
_VOL_LISTENER_PATCH: Optional[Any] = None
_NOTIF_PATCH: Optional[Any] = None


def setUpModule() -> None:
    global _TEST_TEMP_CONFIG_DIR, _VOL_LISTENER_PATCH, _NOTIF_PATCH
    _TEST_TEMP_CONFIG_DIR = tempfile.TemporaryDirectory()
    os.environ["XDG_CONFIG_HOME"] = _TEST_TEMP_CONFIG_DIR.name
    import config
    config._CONFIG_DIR = os.path.join(_TEST_TEMP_CONFIG_DIR.name, "smolplayer")
    config._CONFIG_FILE = os.path.join(config._CONFIG_DIR, "config.ini")
    config._STATE_FILE = os.path.join(config._CONFIG_DIR, "state.json")

    import player
    _VOL_LISTENER_PATCH = patch.object(player.PlayerEngine, "_start_volume_listener")
    _VOL_LISTENER_PATCH.start()

    import utils
    _NOTIF_PATCH = patch.object(utils, "send_notification")
    _NOTIF_PATCH.start()


def tearDownModule() -> None:
    global _TEST_TEMP_CONFIG_DIR, _VOL_LISTENER_PATCH, _NOTIF_PATCH
    if _NOTIF_PATCH is not None:
        _NOTIF_PATCH.stop()
        _NOTIF_PATCH = None
    if _VOL_LISTENER_PATCH is not None:
        _VOL_LISTENER_PATCH.stop()
        _VOL_LISTENER_PATCH = None
    if _TEST_TEMP_CONFIG_DIR is not None:
        _TEST_TEMP_CONFIG_DIR.cleanup()
        _TEST_TEMP_CONFIG_DIR = None
    if _ORIGINAL_XDG_CONFIG_HOME is not None:
        os.environ["XDG_CONFIG_HOME"] = _ORIGINAL_XDG_CONFIG_HOME
    else:
        os.environ.pop("XDG_CONFIG_HOME", None)
    import config
    config._CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "smolplayer")
    config._CONFIG_FILE = os.path.join(config._CONFIG_DIR, "config.ini")
    config._STATE_FILE = os.path.join(config._CONFIG_DIR, "state.json")


class TestModuleStructure(unittest.TestCase):
    def test_direct_imports_and_reexports(self) -> None:
        import player
        import playlist
        self.assertIs(player.TrackInfo, probing.TrackInfo)
        self.assertIs(player.probe_track, probing.probe_track)
        self.assertIs(player.extract_cover, probing.extract_cover)
        self.assertIs(player.DacCapabilities, dac.DacCapabilities)
        self.assertIs(player.get_system_sink_info, dac.get_system_sink_info)
        self.assertIs(playlist.parse_cue, parsers.parse_cue)
        self.assertIs(playlist.parse_m3u, parsers.parse_m3u)
        self.assertIs(playlist.parse_pls, parsers.parse_pls)
        self.assertIs(playlist.parse_xspf, parsers.parse_xspf)
        self.assertIs(playlist.scan_audio_files_recursive, parsers.scan_audio_files_recursive)


class TestConfig(unittest.TestCase):
    def test_config_defaults(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.replay_gain, 0)
        self.assertEqual(cfg.shuffle_algo, 0)
        self.assertEqual(cfg.sort_method, 0)
        self.assertEqual(cfg.recurse_fileopen, 0)
        self.assertEqual(cfg.recurse_folderopen, 1)
        self.assertEqual(cfg.timeout, 30)
        self.assertEqual(cfg.presence, 0)
        self.assertEqual(cfg.internal_resampler, 0)
        self.assertEqual(cfg.gapless_playback, 0)
        self.assertEqual(cfg.cue_noshuffle, 1)
        self.assertEqual(cfg.cue_order, 0)
        self.assertEqual(cfg.lossy_bps, 0)
        self.assertTrue(cfg.tray_enabled)
        self.assertTrue(cfg.notifications_enabled)

    def test_config_presence_flags(self) -> None:
        c1 = Config(presence=1)
        self.assertTrue(c1.tray_enabled)
        self.assertFalse(c1.notifications_enabled)

        c2 = Config(presence=2)
        self.assertFalse(c2.tray_enabled)
        self.assertTrue(c2.notifications_enabled)

        c3 = Config(presence=3)
        self.assertFalse(c3.tray_enabled)
        self.assertFalse(c3.notifications_enabled)

    def test_remember_toggles_state(self) -> None:
        from config import save_toggles_state, load_toggles_state, normalize_loop_status
        self.assertEqual(normalize_loop_status("off"), "None")
        self.assertEqual(normalize_loop_status("none"), "None")
        self.assertEqual(normalize_loop_status("false"), "None")
        self.assertEqual(normalize_loop_status(False), "None")
        self.assertEqual(normalize_loop_status("track"), "Track")
        self.assertEqual(normalize_loop_status("single"), "Track")
        self.assertEqual(normalize_loop_status("playlist"), "Playlist")
        self.assertEqual(normalize_loop_status("all"), "Playlist")
        self.assertEqual(normalize_loop_status(True), "Playlist")

        save_toggles_state(True, "Playlist")
        shuf, loop = load_toggles_state()
        self.assertTrue(shuf)
        self.assertEqual(loop, "Playlist")

        # Verify saving off / none properly persists
        save_toggles_state(False, "None")
        shuf, loop = load_toggles_state()
        self.assertFalse(shuf)
        self.assertEqual(loop, "None")

    def test_remember_volume_state(self) -> None:
        from config import (
            save_volume_state,
            load_volume_state,
            save_toggles_state,
            load_toggles_state,
        )

        # Default when no volume has been saved
        self.assertEqual(load_volume_state(), 1.0)

        # Save volume and reload
        save_volume_state(0.44)
        self.assertAlmostEqual(load_volume_state(), 0.44)

        # Clamping
        save_volume_state(1.5)
        self.assertEqual(load_volume_state(), 1.0)
        save_volume_state(-0.2)
        self.assertEqual(load_volume_state(), 0.0)

        # State isolation: saving toggles does not wipe volume, and saving volume does not wipe toggles
        save_toggles_state(True, "Track")
        save_volume_state(0.65)
        shuf, loop = load_toggles_state()
        self.assertTrue(shuf)
        self.assertEqual(loop, "Track")
        self.assertAlmostEqual(load_volume_state(), 0.65)

        save_toggles_state(False, "None")
        self.assertAlmostEqual(load_volume_state(), 0.65)

    def test_decode_method_option(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.decode_method, 0)
        c_gst = Config(decode_method=1)
        self.assertEqual(c_gst.decode_method, 1)

    def test_config_file_parsing(self) -> None:
        import configparser
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".ini") as f:
            f.write("[smolplayer]\n"
                    "replay_gain = 2\n"
                    "shuffle_algo = 1\n"
                    "sort_method = 3\n"
                    "recurse_fileopen = 1\n"
                    "recurse_folderopen = 0\n"
                    "timeout = 15\n"
                    "presence = 2\n"
                    "remember_toggles = 1\n"
                    "decode_method = 1\n"
                    "internal_resampler = 1\n"
                    "gapless_playback = 1\n"
                    "cue_noshuffle = 0\n"
                    "cue_order = 1\n"
                    "lossy_bps = 1\n")
            tmp_path = f.name

        try:
            with patch("config._CONFIG_FILE", tmp_path):
                cfg = get_config()
                self.assertEqual(cfg.replay_gain, 2)
                self.assertEqual(cfg.shuffle_algo, 1)
                self.assertEqual(cfg.sort_method, 3)
                self.assertEqual(cfg.recurse_fileopen, 1)
                self.assertEqual(cfg.recurse_folderopen, 0)
                self.assertEqual(cfg.timeout, 15)
                self.assertEqual(cfg.presence, 2)
                self.assertEqual(cfg.remember_toggles, 1)
                self.assertEqual(cfg.decode_method, 1)
                self.assertEqual(cfg.internal_resampler, 1)
                self.assertEqual(cfg.gapless_playback, 1)
                self.assertEqual(cfg.cue_noshuffle, 0)
                self.assertEqual(cfg.cue_order, 1)
                self.assertEqual(cfg.lossy_bps, 1)
                self.assertFalse(cfg.tray_enabled)
                self.assertTrue(cfg.notifications_enabled)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_config_auto_update_missing_options(self) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".ini") as f:
            f.write("[smolplayer]\nreplay_gain = 1\n")
            tmp_path = f.name

        try:
            with patch("config._CONFIG_FILE", tmp_path):
                cfg = get_config()
                self.assertEqual(cfg.replay_gain, 1)
                self.assertEqual(cfg.replaygain_preamp, 0.0)
                self.assertEqual(cfg.replaygain_default_preamp, 0.0)
                self.assertEqual(cfg.cue_noshuffle, 1)
                self.assertEqual(cfg.cue_order, 0)
                self.assertEqual(cfg.lossy_bps, 0)
                self.assertEqual(cfg.gapless_playback, 0)
                with open(tmp_path, "r", encoding="utf-8") as f_read:
                    content = f_read.read()
                    self.assertIn("replaygain_preamp = 0", content)
                    self.assertIn("replaygain_default_preamp = 0", content)
                    self.assertIn("cue_noshuffle = 1", content)
                    self.assertIn("cue_order = 0", content)
                    self.assertIn("lossy_bps = 0", content)
                    self.assertIn("gapless_playback = 0", content)
                    self.assertIn("replay_gain = 1", content)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_open_config_file(self) -> None:
        with patch("subprocess.Popen") as mock_popen:
            open_config_file()
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            self.assertEqual(args[0], "xdg-open")
            self.assertEqual(args[1], get_config_file_path())



class TestUtils(unittest.TestCase):
    def test_natural_sort_key(self) -> None:
        files = ["track10.flac", "track2.flac", "track1.flac", "track20.flac"]
        sorted_files = sorted(files, key=natural_sort_key)
        self.assertEqual(sorted_files, ["track1.flac", "track2.flac", "track10.flac", "track20.flac"])

    def test_normalize_file_path(self) -> None:
        self.assertEqual(normalize_file_path("/tmp/test.mp3"), "/tmp/test.mp3")
        self.assertEqual(normalize_file_path("file:///tmp/my%20song.flac"), "/tmp/my song.flac")

    def test_fmt_time(self) -> None:
        self.assertEqual(fmt_time(0.0), "0:00")
        self.assertEqual(fmt_time(65.0), "1:05")
        self.assertEqual(fmt_time(3665.0), "1:01:05")

    def test_write_cover_art(self) -> None:
        png_bytes = b"\x89PNG\r\n\x1a\nfake_png_data"
        uri = write_cover_art(png_bytes)
        self.assertTrue(uri.startswith("file://"))
        self.assertTrue(uri.endswith(".png"))
        path = normalize_file_path(uri)
        self.assertTrue(os.path.exists(path))
        os.remove(path)


class TestPlaylistManager(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = self.temp_dir.name

        # Create nested directory structure with dummy audio files
        self.disc1 = os.path.join(self.root, "Disc 1")
        self.disc2 = os.path.join(self.root, "Disc 2")
        os.makedirs(self.disc1)
        os.makedirs(self.disc2)

        self.f1 = os.path.join(self.disc1, "01.flac")
        self.f2 = os.path.join(self.disc1, "02.flac")
        self.f3 = os.path.join(self.disc2, "01.flac")

        for path in (self.f1, self.f2, self.f3):
            with open(path, "wb") as f:
                f.write(b"dummy audio content")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_scan_audio_files_recursive(self) -> None:
        found = scan_audio_files_recursive(self.root)
        self.assertEqual(len(found), 3)
        self.assertEqual(found[0], self.f1)
        self.assertEqual(found[1], self.f2)
        self.assertEqual(found[2], self.f3)

    def test_single_file_double_click_non_recursive(self) -> None:
        pm = PlaylistManager()
        first_track = pm.load_file_or_folder(self.f1)
        self.assertEqual(first_track, self.f1)
        pm._scan_folder_sync(self.disc1, self.f1, recursive=False)
        self.assertEqual(len(pm), 2)
        self.assertEqual(pm._items, [self.f1, self.f2])

    def test_playlist_navigation_and_looping(self) -> None:
        pm = PlaylistManager()
        first_track = pm.load_file_or_folder(self.root)
        self.assertEqual(first_track, self.f1)
        self.assertEqual(len(pm), 3)

        # Advance tracks
        self.assertEqual(pm.get_next(), self.f2)
        self.assertEqual(pm.get_next(), self.f3)

        # Loop status None -> None after end
        pm.loop_status = "None"
        self.assertIsNone(pm.get_next())

        # Loop status Playlist -> Wraps around
        pm.loop_status = "Playlist"
        self.assertEqual(pm.get_next(), self.f1)

        # Setting loop status to 'off' or 'none' sets it back to 'None'
        pm.loop_status = "off"
        self.assertEqual(pm.loop_status, "None")

    def test_shuffle_mode(self) -> None:
        with patch("playlist.get_config", return_value=Config(remember_toggles=0)):
            pm = PlaylistManager()
            pm.load_file_or_folder(self.root)
            pm.shuffle = True

            self.assertTrue(pm.shuffle)
            self.assertEqual(len(pm._shuffled_indices), 3)
            # Current playing track must stay at position 0 in shuffled queue
            self.assertEqual(pm._shuffled_indices[0], 0)

            pm.shuffle = False
            self.assertFalse(pm.shuffle)

    def test_sort_methods(self) -> None:
        from playlist import sort_audio_files
        files = [self.f3, self.f1, self.f2]

        with patch("player._probe_metadata") as mock_meta:
            mock_meta.side_effect = lambda p: {
                self.f1: {"title": "Alpha", "artist": "Zebra", "disc": 1, "track": 1},
                self.f2: {"title": "Beta", "artist": "Zebra", "disc": 1, "track": 2},
                self.f3: {"title": "Gamma", "artist": "Apple", "disc": 2, "track": 1},
            }.get(p)

            # Sort by Title (method 1)
            sorted_title = sort_audio_files(list(files), sort_method=1)
            self.assertEqual([os.path.basename(p) for p in sorted_title], ["01.flac", "02.flac", "01.flac"])

            # Sort by Artist & Title (method 3)
            sorted_artist = sort_audio_files(list(files), sort_method=3)
            self.assertEqual(sorted_artist[0], self.f3)  # Apple first

    def test_remember_toggles_across_play_sessions(self) -> None:
        # Case 1: remember_toggles == 0 (default: resets on every new play session)
        with patch("playlist.get_config", return_value=Config(remember_toggles=0)):
            pm = PlaylistManager()
            pm.load_file_or_folder(self.root)
            pm.shuffle = True
            pm.loop_status = "Track"
            self.assertTrue(pm.shuffle)
            self.assertEqual(pm.loop_status, "Track")

            # Starting a new play session resets toggles to False / "None"
            pm.load_file_or_folder(self.f1)
            self.assertFalse(pm.shuffle)
            self.assertEqual(pm.loop_status, "None")

        # Case 2: remember_toggles == 1 (persists across play sessions and app opens)
        with patch("playlist.get_config", return_value=Config(remember_toggles=1)):
            pm = PlaylistManager()
            pm.load_file_or_folder(self.root)
            pm.shuffle = True
            pm.loop_status = "Playlist"
            self.assertTrue(pm.shuffle)
            self.assertEqual(pm.loop_status, "Playlist")

            # Starting a new play session keeps the toggles
            pm.load_file_or_folder(self.f2)
            self.assertTrue(pm.shuffle)
            self.assertEqual(pm.loop_status, "Playlist")

    def test_peek_next(self) -> None:
        pm = PlaylistManager()
        pm.load_file_or_folder(self.root)
        self.assertEqual(pm.current_index, 0)

        # In linear mode, peek_next returns track 1 without advancing index
        next_t = pm.peek_next()
        self.assertEqual(next_t, self.f2)
        self.assertEqual(pm.current_index, 0)

        # Advance to last track
        pm.get_next()
        pm.get_next()
        self.assertEqual(pm.current_index, 2)
        self.assertIsNone(pm.peek_next())

        # With Playlist loop, peek_next returns first track
        pm.loop_status = "Playlist"
        self.assertEqual(pm.peek_next(), self.f1)

        # With Track loop, peek_next returns current track
        pm.loop_status = "Track"
        self.assertEqual(pm.peek_next(auto_advance=True), self.f3)


class TestPlayerEngine(unittest.TestCase):
    def setUp(self) -> None:
        from config import save_volume_state
        save_volume_state(1.0)
        self.engine = PlayerEngine()

    def tearDown(self) -> None:
        self.engine.close()
        from config import save_volume_state
        save_volume_state(1.0)

    def test_engine_initial_state(self) -> None:
        with patch.object(self.engine, "sync_system_volume"):
            self.assertEqual(self.engine.state, STATE_STOPPED)
            self.assertEqual(self.engine.volume, 1.0)
            self.assertIsNone(self.engine.track)


    def test_volume_clamping(self) -> None:
        from config import save_volume_state
        self.engine.set_volume(1.5)
        self.assertLessEqual(self.engine.volume, 1.0)

        self.engine.set_volume(-0.5)
        self.assertGreaterEqual(self.engine.volume, 0.0)
        save_volume_state(1.0)

    def test_volume_persistence_across_sessions(self) -> None:
        from config import save_volume_state, load_volume_state
        self.engine.set_volume(0.42)
        self.assertAlmostEqual(self.engine.volume, 0.42)
        self.assertAlmostEqual(load_volume_state(), 0.42)

        # New engine instance in next play session loads the saved volume
        new_engine = PlayerEngine()
        try:
            with patch.object(new_engine, "sync_system_volume"):
                self.assertAlmostEqual(new_engine.volume, 0.42)
        finally:
            new_engine.close()

        # Reset volume back to 1.0
        save_volume_state(1.0)

    def test_launch_pipeline_applies_volume_to_new_pwcat(self) -> None:
        t = TrackInfo(path="/tmp/fake.flac", duration=180.0)
        self.engine._track = t
        self.engine.set_volume(0.55)

        with patch("subprocess.Popen") as mock_popen, \
             patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch.object(self.engine, "_apply_volume") as mock_apply_vol:
            mock_popen.return_value.poll.return_value = None
            self.engine._launch_pipeline()
            mock_apply_vol.assert_called_once()

    def test_replaygain_cmd_args(self) -> None:
        # Off: No gain filter applied
        t_no_gain = TrackInfo(path="/tmp/fake.flac", duration=180.0)
        self.engine.replay_gain = 0
        self.engine.replaygain_preamp = 3.0
        self.engine.replaygain_default_preamp = -2.0
        with patch("subprocess.Popen") as mock_popen, patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            mock_popen.return_value.poll.return_value = None
            proc = self.engine._spawn_ffmpeg_proc(t_no_gain, 0.0)
            self.assertIsNotNone(proc)
            cmd = mock_popen.call_args[0][0]
            self.assertNotIn("-af", cmd)

        # Track Gain ON with RG info and preamp
        self.engine.replay_gain = 1
        t_gain = TrackInfo(path="/tmp/fake.flac", duration=180.0, track_gain_db=-3.50)
        with patch("subprocess.Popen") as mock_popen, patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            mock_popen.return_value.poll.return_value = None
            proc = self.engine._spawn_ffmpeg_proc(t_gain, 0.0)
            self.assertIsNotNone(proc)
            cmd = mock_popen.call_args[0][0]
            self.assertIn("-af", cmd)
            self.assertIn("volume=-0.50dB", cmd[cmd.index("-af") + 1])  # -3.50 + 3.0 = -0.50

        # Track Gain ON without RG info (uses default preamp)
        with patch("subprocess.Popen") as mock_popen, patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            mock_popen.return_value.poll.return_value = None
            proc = self.engine._spawn_ffmpeg_proc(t_no_gain, 0.0)
            self.assertIsNotNone(proc)
            cmd = mock_popen.call_args[0][0]
            self.assertIn("-af", cmd)
            self.assertIn("volume=-2.00dB", cmd[cmd.index("-af") + 1])

    def test_replaygain_peaking_modes(self) -> None:
        self.engine.replay_gain = 1
        self.engine.replaygain_preamp = 0.0
        t_peaking = TrackInfo(path="/tmp/fake.flac", duration=180.0, track_gain_db=6.0, track_peak=1.0)

        # Mode 0 (Disabled) -> gain is +6.00 dB, no limiter
        self.engine.replaygain_peaking = 0
        self.assertEqual(self.engine._calc_replaygain_db(t_peaking), 6.0)
        with patch("subprocess.Popen") as mock_popen, patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            mock_popen.return_value.poll.return_value = None
            self.engine._spawn_ffmpeg_proc(t_peaking, 0.0)
            cmd = mock_popen.call_args[0][0]
            self.assertIn("volume=6.00dB", cmd[cmd.index("-af") + 1])
            self.assertNotIn("alimiter", cmd[cmd.index("-af") + 1])

        # Mode 1 (Appropriate gain) -> gain reduced to 0.0 dB to prevent clipping on 1.0 peak
        self.engine.replaygain_peaking = 1
        self.assertAlmostEqual(self.engine._calc_replaygain_db(t_peaking), 0.0, places=2)

        # Mode 2 (Dynamic range compression) -> gain is +6.00 dB, but alimiter is appended because it clips
        self.engine.replaygain_peaking = 2
        self.assertEqual(self.engine._calc_replaygain_db(t_peaking), 6.0)
        with patch("subprocess.Popen") as mock_popen, patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            mock_popen.return_value.poll.return_value = None
            self.engine._spawn_ffmpeg_proc(t_peaking, 0.0)
            cmd = mock_popen.call_args[0][0]
            af = cmd[cmd.index("-af") + 1]
            self.assertIn("volume=6.00dB", af)
            self.assertIn("alimiter=limit=1.0", af)

        # Mode 2 with GStreamer
        with patch("subprocess.Popen") as mock_popen, \
             patch("shutil.which", return_value="/usr/bin/gst-launch-1.0"), \
             patch("player._get_pactl_output", return_value=("alsa_output", "")):
            mock_popen.return_value.poll.return_value = None
            self.engine._spawn_gstreamer_proc(t_peaking, 0.0)
            py_call = None
            for call_item in mock_popen.call_args_list:
                args = call_item[0][0]
                if isinstance(args, list) and len(args) > 2 and args[0] == sys.executable:
                    py_call = args
                    break
            self.assertIsNotNone(py_call)
            self.assertIn("audiodynamic", py_call[2])

        # Test user preamp adjustment on ReplayGain track with peaking
        self.engine.replaygain_preamp = 3.0  # total gain = 6.0 + 3.0 = 9.0 dB
        self.engine.replaygain_peaking = 1
        self.assertAlmostEqual(self.engine._calc_replaygain_db(t_peaking), 0.0, places=2)

        # Test user default_preamp adjustment on Non-ReplayGain track with peaking
        t_no_rg = TrackInfo(path="/tmp/fake_norg.flac", duration=180.0)
        self.engine.replaygain_default_preamp = 4.0  # positive default preamp on non-RG track
        self.engine.replaygain_peaking = 0
        self.assertEqual(self.engine._calc_replaygain_db(t_no_rg), 4.0)

        # Mode 1 clamps positive default preamp to 0.0 to prevent clipping
        self.engine.replaygain_peaking = 1
        self.assertEqual(self.engine._calc_replaygain_db(t_no_rg), 0.0)

        # Mode 2 applies positive default preamp (+4.0 dB) with limiter enabled
        self.engine.replaygain_peaking = 2
        self.assertEqual(self.engine._calc_replaygain_db(t_no_rg), 4.0)
        self.assertTrue(self.engine._replaygain_should_compress(t_no_rg, 4.0))

        # Negative default preamp (-3.0 dB) does not clip and needs no compression
        self.engine.replaygain_default_preamp = -3.0
        self.engine.replaygain_peaking = 1
        self.assertEqual(self.engine._calc_replaygain_db(t_no_rg), -3.0)
        self.engine.replaygain_peaking = 2
        self.assertEqual(self.engine._calc_replaygain_db(t_no_rg), -3.0)
        self.assertFalse(self.engine._replaygain_should_compress(t_no_rg, -3.0))

    def test_replaygain_priority_over_r128(self) -> None:
        ffprobe_output = {
            "format": {
                "duration": "120.0",
                "tags": {
                    "R128_TRACK_GAIN": "-8.5 dB",
                    "REPLAYGAIN_TRACK_GAIN": "-4.2 dB",
                    "R128_ALBUM_GAIN": "-7.0 dB",
                    "REPLAYGAIN_ALBUM_GAIN": "-3.1 dB",
                    "R128_TRACK_PEAK": "0.95",
                    "REPLAYGAIN_TRACK_PEAK": "0.85",
                }
            },
            "streams": [{
                "sample_rate": "44100",
                "channels": 2,
                "sample_fmt": "s16",
                "tags": {}
            }]
        }
        with patch("subprocess.run") as mock_run:
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_res.stdout = json.dumps(ffprobe_output)
            mock_run.return_value = mock_res

            meta = _probe_metadata("/tmp/fake_both_tags.flac")
            self.assertIsNotNone(meta)
            self.assertEqual(meta["track_gain_db"], -4.2)
            self.assertEqual(meta["album_gain_db"], -3.1)
            self.assertEqual(meta["track_peak"], 0.85)

    def test_16bit_replaygain_peak_normalization(self) -> None:
        # Legacy 16-bit tags with raw sample peak value (e.g. 32767)
        ffprobe_16bit = {
            "format": {
                "duration": "120.0",
                "tags": {
                    "REPLAYGAIN_TRACK_GAIN": "-2.5 dB",
                    "REPLAYGAIN_TRACK_PEAK": "32768",
                }
            },
            "streams": [{
                "sample_rate": "44100",
                "channels": 2,
                "sample_fmt": "s16",
                "tags": {}
            }]
        }
        with patch("subprocess.run") as mock_run:
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_res.stdout = json.dumps(ffprobe_16bit)
            mock_run.return_value = mock_res

            meta = _probe_metadata("/tmp/fake_16bit.flac")
            self.assertIsNotNone(meta)
            self.assertEqual(meta["track_gain_db"], -2.5)
            self.assertEqual(meta["track_peak"], 1.0)  # 32768 / 32768.0 = 1.0

    def test_bit_perfect_no_audio_processing(self) -> None:
        t = TrackInfo(
            path="/tmp/fake.flac",
            duration=180.0,
            track_gain_db=6.0,
            track_peak=1.0,
            sample_rate=96000,
            channels=2,
            ffmpeg_fmt="s32le",
            pwcat_fmt="s32",
        )
        self.engine.replay_gain = 1
        self.engine.replaygain_preamp = 3.0
        self.engine.replaygain_peaking = 2
        self.engine.internal_resampler = 1
        self.engine.bit_perfect = 1

        # FFmpeg in bit-perfect mode must NOT contain any -af filter
        with patch("subprocess.Popen") as mock_popen, patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            mock_popen.return_value.poll.return_value = None
            self.engine._spawn_ffmpeg_proc(t, 0.0, is_bit_perfect=True)
            cmd = mock_popen.call_args[0][0]
            self.assertNotIn("-af", cmd)
            self.assertIn("-f", cmd)
            self.assertEqual(cmd[cmd.index("-f") + 1], "s32le")

        # GStreamer in bit-perfect mode must NOT contain volume, audiodynamic, or resampler
        with patch("subprocess.Popen") as mock_popen, \
             patch("shutil.which", return_value="/usr/bin/gst-launch-1.0"):
            mock_popen.return_value.poll.return_value = None
            self.engine._spawn_gstreamer_proc(t, 0.0, is_bit_perfect=True)
            py_call = mock_popen.call_args_list[0][0][0]
            self.assertNotIn("volume", py_call[2])
            self.assertNotIn("audiodynamic", py_call[2])
            self.assertNotIn("audioresample", py_call[2])
            self.assertIn("audio/x-raw,format=S32LE,layout=interleaved", py_call[2])

    def test_lossy_source_detection(self) -> None:
        self.assertTrue(is_lossy_source("/path/song.mp3", codec_name="mp3"))
        self.assertTrue(is_lossy_source("/path/song.opus", codec_name="opus"))
        self.assertTrue(is_lossy_source("/path/song.ogg", codec_name="vorbis"))
        self.assertTrue(is_lossy_source("/path/song.m4a", codec_name="aac"))
        self.assertTrue(is_lossy_source("/path/song.wma", codec_name="wmav2"))
        self.assertFalse(is_lossy_source("/path/song.flac", codec_name="flac"))
        self.assertFalse(is_lossy_source("/path/song.wav", codec_name="pcm_s16le"))
        self.assertFalse(is_lossy_source("/path/song.wav", codec_name="pcm_f32le", sample_fmt="flt"))
        self.assertFalse(is_lossy_source("/path/song.m4a", codec_name="alac"))

    def test_lossy_bps_probe_override(self) -> None:
        ffprobe_mp3 = {
            "format": {"duration": "180.0"},
            "streams": [{
                "codec_name": "mp3",
                "sample_rate": "44100",
                "channels": 2,
                "sample_fmt": "fltp",
                "tags": {"title": "MP3 Track"}
            }]
        }

        # With lossy_bps = 0 (default 32-bit float)
        with patch("config.get_config", return_value=Config(lossy_bps=0)), \
             patch("player.get_config", return_value=Config(lossy_bps=0)), \
             patch("subprocess.run") as mock_run:
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_res.stdout = json.dumps(ffprobe_mp3)
            mock_run.return_value = mock_res

            meta = _probe_metadata("/tmp/song.mp3")
            self.assertEqual(meta["ffmpeg_fmt"], "f32le")
            self.assertEqual(meta["pwcat_fmt"], "f32")
            self.assertEqual(meta["bytes_per_sample"], 4)

        # With lossy_bps = 1 (16 bit)
        with patch("config.get_config", return_value=Config(lossy_bps=1)), \
             patch("player.get_config", return_value=Config(lossy_bps=1)), \
             patch("subprocess.run") as mock_run:
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_res.stdout = json.dumps(ffprobe_mp3)
            mock_run.return_value = mock_res

            meta = _probe_metadata("/tmp/song.mp3")
            self.assertEqual(meta["ffmpeg_fmt"], "s16le")
            self.assertEqual(meta["pwcat_fmt"], "s16")
            self.assertEqual(meta["bytes_per_sample"], 2)

    def test_lossy_bps_ffmpeg_spawn_args(self) -> None:
        t = TrackInfo(
            path="/tmp/song.mp3",
            duration=180.0,
            sample_rate=44100,
            channels=2,
            ffmpeg_fmt="s16le",
            pwcat_fmt="s16",
            bytes_per_sample=2,
        )
        with patch("subprocess.Popen") as mock_popen, patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            mock_popen.return_value.poll.return_value = None
            self.engine._spawn_ffmpeg_proc(t, 0.0)
            cmd = mock_popen.call_args[0][0]
            self.assertIn("-f", cmd)
            self.assertEqual(cmd[cmd.index("-f") + 1], "s16le")

    def test_audiophile_mode_cmd_args(self) -> None:
        t = TrackInfo(path="/tmp/fake.flac", duration=180.0)
        self.engine.replay_gain = 0
        self.engine.audiophile_mode = 1

        with patch("subprocess.Popen") as mock_popen, patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            mock_popen.return_value.poll.return_value = None
            proc = self.engine._spawn_ffmpeg_proc(t, 0.0)
            self.assertIsNotNone(proc)
            cmd = mock_popen.call_args[0][0]
            self.assertIn("-af", cmd)
            af_arg = cmd[cmd.index("-af") + 1]
            self.assertIn("resampler=soxr:precision=33:dither_method=triangular", af_arg)

    def test_get_system_sink_info_default_sink_matching(self) -> None:
        pactl_info = "Server Name: PulseAudio (on PipeWire 1.6.8)\nDefault Sink: alsa_output.usb-ZiShan_Z3\n"
        pactl_sinks = (
            "Name: alsa_output.pci-hdmi\n"
            "Sample Specification: s32le 2ch 48000Hz\n\n"
            "Name: alsa_output.usb-ZiShan_Z3\n"
            "Sample Specification: s16le 2ch 48000Hz\n"
        )
        def fake_run(cmd, capture_output=True, text=True, timeout=2):
            mock_res = MagicMock()
            mock_res.returncode = 0
            if "info" in cmd:
                mock_res.stdout = pactl_info
            else:
                mock_res.stdout = pactl_sinks
            return mock_res

        with patch("subprocess.run", side_effect=fake_run):
            pw_fmt, rate = get_system_sink_info()
            self.assertEqual(pw_fmt, "s16")
            self.assertEqual(rate, 48000)

    def test_audiophile_mode_format_and_rate_matching(self) -> None:
        t = TrackInfo(path="/tmp/fake_24bit.flac", duration=180.0, pwcat_fmt="s32", sample_rate=44100)
        self.engine.replay_gain = 0
        self.engine.audiophile_mode = 1

        with patch("player.get_system_sink_info", return_value=("s16", 48000)), \
             patch("subprocess.Popen") as mock_popen, \
             patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            mock_popen.return_value.poll.return_value = None
            proc = self.engine._spawn_ffmpeg_proc(t, 0.0)
            self.assertIsNotNone(proc)
            cmd = mock_popen.call_args[0][0]
            self.assertIn("-f", cmd)
            self.assertEqual(cmd[cmd.index("-f") + 1], "f32le")

            # Launch pipeline pw-cat arguments test
            self.engine._track = t
            self.engine._launch_pipeline()
            pwcat_call = None
            for call_item in mock_popen.call_args_list:
                args = call_item[0][0]
                if isinstance(args, list) and len(args) > 0 and args[0] == "pw-cat":
                    pwcat_call = args
                    break
            self.assertIsNotNone(pwcat_call)
            self.assertIn("--format", pwcat_call)
            self.assertEqual(pwcat_call[pwcat_call.index("--format") + 1], "f32")
            self.assertIn("--rate", pwcat_call)
            self.assertEqual(pwcat_call[pwcat_call.index("--rate") + 1], "48000")

        # Also verify GStreamer produces F32LE caps
        with patch("player.get_system_sink_info", return_value=("s16", 48000)), \
             patch("subprocess.Popen") as mock_popen, \
             patch("shutil.which", return_value="/usr/bin/gst-launch-1.0"):
            mock_popen.return_value.poll.return_value = None
            self.engine._spawn_gstreamer_proc(t, 0.0)
            py_call = mock_popen.call_args_list[0][0][0]
            self.assertIn("audio/x-raw,format=F32LE,layout=interleaved", py_call[2])
            self.assertIn("audioresample quality=10 ! capsfilter caps=audio/x-raw,rate=48000", py_call[2])

    def test_gapless_playback_seamless_transition(self) -> None:
        t1 = TrackInfo(path="/tmp/track1.flac", title="Track 1", sample_rate=44100, pwcat_fmt="s16", channels=2)
        t2 = TrackInfo(path="/tmp/track2.flac", title="Track 2", sample_rate=44100, pwcat_fmt="s16", channels=2)

        self.engine.gapless_playback = 1
        self.engine._track = t1
        self.engine._pipeline_generation = 1
        iter_next = iter([t2, None])
        self.engine.get_next_track = lambda: next(iter_next, None)

        transitions = []
        self.engine.on_gapless_track_transition = lambda t: transitions.append(t)

        mock_ffmpeg1 = MagicMock()
        mock_ffmpeg1.stdout.read.side_effect = [b"chunk1", b""]
        mock_ffmpeg2 = MagicMock()
        mock_ffmpeg2.stdout.read.side_effect = [b"chunk2", b""]

        mock_pwcat = MagicMock()
        mock_pwcat.stdin = MagicMock()

        self.engine._pwcat = mock_pwcat
        self.engine._pwcat_fmt = ("s16", 44100, 2)

        with patch.object(self.engine, "_spawn_ffmpeg_proc", return_value=mock_ffmpeg2):
            self.engine._pump_loop(
                gen=1,
                stop_evt=threading.Event(),
                ffmpeg_proc=mock_ffmpeg1,
                pwcat_proc=mock_pwcat,
            )

        self.assertEqual(self.engine.track, t2)
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0].title, "Track 2")
        mock_pwcat.stdin.write.assert_any_call(b"chunk1")
        mock_pwcat.stdin.write.assert_any_call(b"chunk2")

    def test_gapless_playback_incompatible_rate_fallback(self) -> None:
        t1 = TrackInfo(path="/tmp/track1.flac", title="Track 1", sample_rate=44100, pwcat_fmt="s16", channels=2)
        t2 = TrackInfo(path="/tmp/track2.flac", title="Track 2", sample_rate=96000, pwcat_fmt="s32", channels=2)

        self.engine.gapless_playback = 1
        self.engine.internal_resampler = 0
        self.engine._track = t1
        self.engine._pipeline_generation = 1
        iter_next = iter([t2, None])
        self.engine.get_next_track = lambda: next(iter_next, None)

        track_ended = []
        self.engine.on_track_end = lambda: track_ended.append(True)

        mock_ffmpeg1 = MagicMock()
        mock_ffmpeg1.stdout.read.side_effect = [b"chunk1", b""]

        mock_pwcat = MagicMock()
        mock_pwcat.stdin = MagicMock()

        self.engine._pwcat = mock_pwcat
        self.engine._pwcat_fmt = ("s16", 44100, 2)

        self.engine._pump_loop(
            gen=1,
            stop_evt=threading.Event(),
            ffmpeg_proc=mock_ffmpeg1,
            pwcat_proc=mock_pwcat,
        )

        self.assertEqual(len(track_ended), 1)
        self.assertEqual(self.engine.track, t1)

    def test_gapless_playback_with_internal_resampler_chains_different_rates(self) -> None:
        t1 = TrackInfo(path="/tmp/track1.flac", title="Track 1", sample_rate=44100, pwcat_fmt="s16", channels=2)
        t2 = TrackInfo(path="/tmp/track2.flac", title="Track 2", sample_rate=96000, pwcat_fmt="s32", channels=2)

        self.engine.gapless_playback = 1
        self.engine.internal_resampler = 1
        self.engine._track = t1
        self.engine._pipeline_generation = 1
        iter_next = iter([t2, None])
        self.engine.get_next_track = lambda: next(iter_next, None)

        transitions = []
        self.engine.on_gapless_track_transition = lambda t: transitions.append(t)

        mock_ffmpeg1 = MagicMock()
        mock_ffmpeg1.stdout.read.side_effect = [b"chunk1", b""]
        mock_ffmpeg2 = MagicMock()
        mock_ffmpeg2.stdout.read.side_effect = [b"chunk2", b""]

        mock_pwcat = MagicMock()
        mock_pwcat.stdin = MagicMock()

        self.engine._pwcat = mock_pwcat
        self.engine._pwcat_fmt = ("f32", 48000, 2)

        with patch.object(self.engine, "_spawn_ffmpeg_proc", return_value=mock_ffmpeg2):
            self.engine._pump_loop(
                gen=1,
                stop_evt=threading.Event(),
                ffmpeg_proc=mock_ffmpeg1,
                pwcat_proc=mock_pwcat,
            )

        self.assertEqual(self.engine.track, t2)
        self.assertEqual(len(transitions), 1)

    def test_bit_perfect_passthrough_and_fallback(self) -> None:
        t = TrackInfo(path="/tmp/fake.flac", duration=180.0)
        self.engine._track = t
        self.engine.bit_perfect = 1

        def fake_which(bin_name):
            return f"/usr/bin/{bin_name}"

        with patch("subprocess.Popen") as mock_popen, \
             patch("shutil.which", side_effect=fake_which), \
             patch("player.get_alsa_hw_device", return_value="plughw:0,0"):
            mock_popen.return_value.poll.return_value = None
            self.engine._pwcat = None
            self.engine._launch_pipeline()
            aplay_call = None
            for call_item in mock_popen.call_args_list:
                args = call_item[0][0]
                if isinstance(args, list) and len(args) > 0 and args[0] == "aplay":
                    aplay_call = args
                    break
            self.assertIsNotNone(aplay_call)
            self.assertIn("-D", aplay_call)
            self.assertEqual(aplay_call[aplay_call.index("-D") + 1], "plughw:0,0")

        call_count = 0
        def fake_popen(cmd, **kwargs):
            nonlocal call_count
            mock_proc = MagicMock()
            if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "aplay":
                call_count += 1
                mock_proc.poll.return_value = 1
                mock_proc.returncode = 1
            else:
                mock_proc.poll.return_value = None
                mock_proc.returncode = 0
            return mock_proc

        with patch("subprocess.Popen", side_effect=fake_popen), \
             patch("shutil.which", side_effect=fake_which), \
             patch("player.send_error_notification") as mock_notif:
            self.engine._pwcat = None
            res = self.engine._launch_pipeline()
            self.assertTrue(res)
            mock_notif.assert_called_once()
            self.assertIn("Bit-perfect playback failed", mock_notif.call_args[0][1])

    def test_decoder_fallback_when_ffmpeg_missing(self) -> None:
        t = TrackInfo(path="/tmp/fake.flac", duration=180.0)
        self.engine._track = t

        def fake_which(cmd: str):
            if cmd == "gst-launch-1.0":
                return "/usr/bin/gst-launch-1.0"
            return None

        with patch("shutil.which", side_effect=fake_which), \
             patch("player._get_pactl_output", return_value=("alsa_output", "")), \
             patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = None
            proc = self.engine._spawn_gstreamer_proc(t, 0.0)
            self.assertIsNotNone(proc)
            py_call = None
            for call_item in mock_popen.call_args_list:
                args = call_item[0][0]
                if isinstance(args, list) and len(args) > 0 and args[0] == sys.executable:
                    py_call = args
                    break
            self.assertIsNotNone(py_call)

    def test_gstreamer_seeking_cmd_args(self) -> None:
        t = TrackInfo(path="/tmp/fake.flac", duration=180.0)
        with patch("shutil.which", return_value="/usr/bin/gst-launch-1.0"), \
             patch("player._get_pactl_output", return_value=("alsa_output", "")), \
             patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = None
            proc = self.engine._spawn_gstreamer_proc(t, 30.0)
            self.assertIsNotNone(proc)
            py_call = None
            for call_item in mock_popen.call_args_list:
                args = call_item[0][0]
                if isinstance(args, list) and len(args) > 2 and args[0] == sys.executable:
                    py_call = args
                    break
            self.assertIsNotNone(py_call)
            self.assertIn("p.seek_simple", py_call[2])
            self.assertIn("30.0 * 1e9", py_call[2])

    def test_both_decoders_failed_notification(self) -> None:
        t = TrackInfo(path="/tmp/fake.flac", duration=180.0)
        self.engine._track = t

        with patch("shutil.which", return_value=None), \
             patch("utils.send_notification") as mock_notify:
            res = self.engine._launch_pipeline()
            self.assertFalse(res)
            self.assertTrue(mock_notify.called)

    def test_rapid_seek_generation_safety(self) -> None:
        # Create dummy track info
        t = TrackInfo(
            path="/tmp/fake.flac",
            title="Fake",
            artist="Artist",
            album="Album",
            duration=180.0,
            sample_rate=44100,
            channels=2,
            pwcat_fmt="s16",
            ffmpeg_fmt="s16le",
            bytes_per_sample=2,
        )
        self.engine._track = t
        self.engine._state = STATE_PLAYING

        initial_gen = self.engine._pipeline_generation
        for _ in range(20):
            with patch("subprocess.Popen") as mock_popen:
                mock_popen.return_value.poll.return_value = None
                mock_popen.return_value.stdout = MagicMock()
                mock_popen.return_value.stdin = MagicMock()
                self.engine.seek(10.0)

        self.assertEqual(self.engine._pipeline_generation, initial_gen + 20)
        self.assertEqual(self.engine.state, STATE_PLAYING)

    def test_rapid_concurrent_seek_coalescing(self) -> None:
        t = TrackInfo(
            path="/tmp/fake.flac",
            title="Fake",
            duration=180.0,
            sample_rate=44100,
            channels=2,
            pwcat_fmt="s16",
            ffmpeg_fmt="s16le",
            bytes_per_sample=2,
        )
        self.engine._track = t
        self.engine._state = STATE_PLAYING

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = None
            mock_popen.return_value.stdout = MagicMock()
            mock_popen.return_value.stdin = MagicMock()

            threads = [
                threading.Thread(target=self.engine.seek, args=(10.0 * i,))
                for i in range(1, 6)
            ]
            for th in threads:
                th.start()
            for th in threads:
                th.join()

            self.assertEqual(self.engine.state, STATE_PLAYING)
            self.assertIn(self.engine._play_start_pos, [10.0, 20.0, 30.0, 40.0, 50.0])
            self.assertGreaterEqual(self.engine.position, 10.0)
            self.assertLessEqual(self.engine.position, 55.0)

    def test_seek_updates_position_immediately_without_stale_reversion(self) -> None:
        t = TrackInfo(path="/tmp/fake.flac", duration=180.0)
        self.engine._track = t
        self.engine._state = STATE_PLAYING
        self.engine._play_start_pos = 10.0
        self.engine._play_start_time = time.monotonic() - 2.0  # 12.0s elapsed

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = None
            mock_popen.return_value.stdout = MagicMock()
            mock_popen.return_value.stdin = MagicMock()

            self.engine.seek(45.0)
            self.assertAlmostEqual(self.engine.position, 45.0, delta=0.5)


class TestTrayService(unittest.TestCase):
    def test_tray_tooltip_text(self) -> None:
        engine = PlayerEngine()
        try:
            playlist = PlaylistManager()

            tray = TrayService(engine, playlist)
            self.assertEqual(tray.get_tooltip_text(), f"{APP_NAME} - Stopped")

            engine._track = TrackInfo(
                path="/tmp/song.mp3",
                title="Fast Car",
                artist="Tracy Chapman",
                album="Album",
                duration=240.0,
                sample_rate=44100,
                channels=2,
                pwcat_fmt="s16",
                ffmpeg_fmt="s16le",
                bytes_per_sample=2,
            )
            engine._state = STATE_PLAYING

            self.assertEqual(tray.get_tooltip_text(), "Fast Car - Tracy Chapman (Playing)")
        finally:
            engine.close()

    def test_tray_menu_events(self) -> None:
        from tray import _DBUS_OK, _MenuObject
        if not _DBUS_OK:
            return
        engine = PlayerEngine()
        try:
            playlist = PlaylistManager()
            mock_open_cfg = MagicMock()
            mock_quit = MagicMock()

            tray = TrayService(engine, playlist, on_quit=mock_quit, on_open_config=mock_open_cfg)
            if hasattr(tray, "_menu_object"):
                menu_obj = tray._menu_object
                # Event for item 1 ("Open Config File")
                menu_obj.Event(1, "clicked", None, 0)
                mock_open_cfg.assert_called_once()

                # Event for item 2 ("Quit smolplayer")
                menu_obj.Event(2, "clicked", None, 0)
                mock_quit.assert_called_once()
        finally:
            engine.close()


class TestMainCLI(unittest.TestCase):
    def test_main_config_flag(self) -> None:
        from main import main
        with patch("sys.argv", ["smolplayer", "--config"]), \
             patch("main.open_config_file") as mock_open, \
             patch("sys.exit", side_effect=SystemExit(0)):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)
            mock_open.assert_called_once()

    def test_main_close_flag(self) -> None:
        from main import main
        with patch("sys.argv", ["smolplayer", "--close"]), \
             patch("main.quit_existing_instance") as mock_quit, \
             patch("sys.exit", side_effect=SystemExit(0)):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)
            mock_quit.assert_called_once()


class TestI18n(unittest.TestCase):
    def test_i18n_fallback_and_translation(self) -> None:
        from i18n import _
        self.assertEqual(_("Open Config File"), "Open Config File")

        import gettext
        loc_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")
        t_es = gettext.translation("smolplayer", localedir=loc_dir, languages=["es"], fallback=True)
        self.assertEqual(t_es.gettext("Open Config File"), "Abrir archivo de configuración")

        t_zh = gettext.translation("smolplayer", localedir=loc_dir, languages=["zh_CN"], fallback=True)
        self.assertEqual(t_zh.gettext("Open Config File"), "打开配置文件")

        t_de = gettext.translation("smolplayer", localedir=loc_dir, languages=["de"], fallback=True)
        self.assertEqual(t_de.gettext("Open Config File"), "Konfigurationsdatei öffnen")

        t_fr = gettext.translation("smolplayer", localedir=loc_dir, languages=["fr"], fallback=True)
        self.assertEqual(t_fr.gettext("Open Config File"), "Ouvrir le fichier de configuration")

        t_ja = gettext.translation("smolplayer", localedir=loc_dir, languages=["ja"], fallback=True)
        self.assertEqual(t_ja.gettext("Open Config File"), "設定ファイルを開く")

        t_pt = gettext.translation("smolplayer", localedir=loc_dir, languages=["pt_BR"], fallback=True)
        self.assertEqual(t_pt.gettext("Open Config File"), "Abrir arquivo de configuração")

        t_ru = gettext.translation("smolplayer", localedir=loc_dir, languages=["ru"], fallback=True)
        self.assertEqual(t_ru.gettext("Open Config File"), "Открыть файл конфигурации")

        t_it = gettext.translation("smolplayer", localedir=loc_dir, languages=["it"], fallback=True)
        self.assertEqual(t_it.gettext("Open Config File"), "Apri file di configurazione")

        t_ar = gettext.translation("smolplayer", localedir=loc_dir, languages=["ar"], fallback=True)
        self.assertEqual(t_ar.gettext("Open Config File"), "فتح ملف التكوين")

        t_ko = gettext.translation("smolplayer", localedir=loc_dir, languages=["ko"], fallback=True)
        self.assertEqual(t_ko.gettext("Open Config File"), "설정 파일 열기")

        t_pl = gettext.translation("smolplayer", localedir=loc_dir, languages=["pl"], fallback=True)
        self.assertEqual(t_pl.gettext("Open Config File"), "Otwórz plik konfiguracyjny")

        t_tr = gettext.translation("smolplayer", localedir=loc_dir, languages=["tr"], fallback=True)
        self.assertEqual(t_tr.gettext("Open Config File"), "Yapılandırma Dosyasını Aç")

        t_nl = gettext.translation("smolplayer", localedir=loc_dir, languages=["nl"], fallback=True)
        self.assertEqual(t_nl.gettext("Open Config File"), "Configuratiebestand openen")




class TestPlaylistParsers(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = self.temp_dir.name

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_cue_time(self) -> None:
        self.assertEqual(parse_cue_time("00:00:00"), 0.0)
        self.assertEqual(parse_cue_time("01:23:45"), 1 * 60 + 23 + 45 / 75.0)
        self.assertEqual(parse_cue_time("03:45"), 225.0)
        self.assertEqual(parse_cue_time("invalid"), 0.0)

    def test_parse_cue_single_file(self) -> None:
        audio_file = os.path.join(self.root, "album.flac")
        with open(audio_file, "wb") as f:
            f.write(b"dummy audio data")

        cue_content = (
            'REM GENRE "Progressive Rock"\n'
            'REM DATE 1973\n'
            'REM REPLAYGAIN_ALBUM_GAIN -6.50 dB\n'
            'REM REPLAYGAIN_ALBUM_PEAK 0.985\n'
            'PERFORMER "Pink Floyd"\n'
            'TITLE "The Dark Side of the Moon"\n'
            'FILE "album.flac" WAVE\n'
            '  TRACK 01 AUDIO\n'
            '    TITLE "Speak to Me"\n'
            '    PERFORMER "Pink Floyd"\n'
            '    INDEX 01 00:00:00\n'
            '  TRACK 02 AUDIO\n'
            '    TITLE "Breathe"\n'
            '    REM REPLAYGAIN_TRACK_GAIN -4.20 dB\n'
            '    REM REPLAYGAIN_TRACK_PEAK 0.920\n'
            '    INDEX 00 01:13:00\n'
            '    INDEX 01 01:15:00\n'
            '  TRACK 03 AUDIO\n'
            '    TITLE "On the Run"\n'
            '    INDEX 01 04:00:00\n'
        )
        cue_file = os.path.join(self.root, "album.cue")
        with open(cue_file, "w", encoding="utf-8") as f:
            f.write(cue_content)

        tracks = parse_cue(cue_file)
        self.assertEqual(len(tracks), 3)

        t1, t2, t3 = tracks
        self.assertEqual(t1.title, "Speak to Me")
        self.assertEqual(t1.artist, "Pink Floyd")
        self.assertEqual(t1.album, "The Dark Side of the Moon")
        self.assertEqual(t1.track_number, 1)
        self.assertEqual(t1.start_time, 0.0)
        self.assertEqual(t1.end_time, 73.0)  # INDEX 00 of Track 2 (01:13:00)
        self.assertEqual(t1.duration, 73.0)
        self.assertEqual(t1.album_gain_db, -6.50)
        self.assertEqual(t1.album_peak, 0.985)

        self.assertEqual(t2.title, "Breathe")
        self.assertEqual(t2.track_number, 2)
        self.assertEqual(t2.start_time, 75.0)  # INDEX 01 of Track 2 (01:15:00)
        self.assertEqual(t2.end_time, 240.0)  # INDEX 01 of Track 3 (04:00:00)
        self.assertEqual(t2.duration, 165.0)
        self.assertEqual(t2.track_gain_db, -4.20)
        self.assertEqual(t2.track_peak, 0.920)

        self.assertEqual(t3.title, "On the Run")
        self.assertEqual(t3.track_number, 3)
        self.assertEqual(t3.start_time, 240.0)
        self.assertIsNone(t3.end_time)

    def test_parse_cue_multi_file(self) -> None:
        f1 = os.path.join(self.root, "side_a.flac")
        f2 = os.path.join(self.root, "side_b.flac")
        for p in (f1, f2):
            with open(p, "wb") as f:
                f.write(b"dummy")

        cue_content = (
            'PERFORMER "Artist"\n'
            'TITLE "Album"\n'
            'FILE "side_a.flac" WAVE\n'
            '  TRACK 01 AUDIO\n'
            '    TITLE "Track 1"\n'
            '    INDEX 01 00:00:00\n'
            'FILE "side_b.flac" WAVE\n'
            '  TRACK 02 AUDIO\n'
            '    TITLE "Track 2"\n'
            '    INDEX 01 00:00:00\n'
        )
        cue_file = os.path.join(self.root, "multi.cue")
        with open(cue_file, "w", encoding="utf-8") as f:
            f.write(cue_content)

        tracks = parse_cue(cue_file)
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].path, f1)
        self.assertEqual(tracks[1].path, f2)

    def test_parse_cue_extension_fallback(self) -> None:
        flac_file = os.path.join(self.root, "cdimage.flac")
        with open(flac_file, "wb") as f:
            f.write(b"dummy")

        cue_content = (
            'PERFORMER "Artist"\n'
            'TITLE "Album"\n'
            'FILE "cdimage.wav" WAVE\n'
            '  TRACK 01 AUDIO\n'
            '    TITLE "Track 1"\n'
            '    INDEX 01 00:00:00\n'
        )
        cue_file = os.path.join(self.root, "cdimage.cue")
        with open(cue_file, "w", encoding="utf-8") as f:
            f.write(cue_content)

        tracks = parse_cue(cue_file)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].path, flac_file)

    def test_parse_m3u_standard_and_extended(self) -> None:
        s1 = os.path.join(self.root, "song1.mp3")
        s2 = os.path.join(self.root, "song2.flac")
        for p in (s1, s2):
            with open(p, "wb") as f:
                f.write(b"audio")

        m3u_content = (
            "#EXTM3U\n"
            "#EXTINF:180,Daft Punk - One More Time\n"
            "#EXTALB:Discovery\n"
            "song1.mp3\n"
            "#EXTINF:240,Aerodynamic\n"
            f"{s2}\n"
        )
        m3u_file = os.path.join(self.root, "test.m3u")
        with open(m3u_file, "w", encoding="utf-8") as f:
            f.write(m3u_content)

        items = parse_m3u(m3u_file)
        self.assertEqual(len(items), 2)
        t1, t2 = items
        self.assertIsInstance(t1, TrackInfo)
        self.assertEqual(t1.path, s1)
        self.assertEqual(t1.title, "One More Time")
        self.assertEqual(t1.artist, "Daft Punk")
        self.assertEqual(t1.album, "Discovery")
        self.assertEqual(t1.duration, 180.0)

        self.assertIsInstance(t2, TrackInfo)
        self.assertEqual(t2.path, s2)
        self.assertEqual(t2.title, "Aerodynamic")
        self.assertEqual(t2.duration, 240.0)

    def test_parse_m3u_latin1(self) -> None:
        s1 = os.path.join(self.root, "canción.mp3")
        with open(s1, "wb") as f:
            f.write(b"audio")

        m3u_content = "#EXTM3U\n#EXTINF:120,Artista - Canción Española\ncanción.mp3\n".encode("latin-1")
        m3u_file = os.path.join(self.root, "latin1.m3u")
        with open(m3u_file, "wb") as f:
            f.write(m3u_content)

        items = parse_m3u(m3u_file)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Canción Española")

    def test_parse_m3u_with_nested_cue(self) -> None:
        audio = os.path.join(self.root, "album.flac")
        with open(audio, "wb") as f:
            f.write(b"audio")

        cue_file = os.path.join(self.root, "album.cue")
        with open(cue_file, "w", encoding="utf-8") as f:
            f.write(
                'TITLE "Album"\n'
                'FILE "album.flac" WAVE\n'
                '  TRACK 01 AUDIO\n'
                '    TITLE "Track 1"\n'
                '    INDEX 01 00:00:00\n'
                '  TRACK 02 AUDIO\n'
                '    TITLE "Track 2"\n'
                '    INDEX 01 01:00:00\n'
            )

        m3u_file = os.path.join(self.root, "all.m3u")
        with open(m3u_file, "w", encoding="utf-8") as f:
            f.write("album.cue\n")

        items = parse_m3u(m3u_file)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "Track 1")
        self.assertEqual(items[1].title, "Track 2")

    def test_parse_pls(self) -> None:
        s1 = os.path.join(self.root, "01.mp3")
        s2 = os.path.join(self.root, "02.mp3")
        for p in (s1, s2):
            with open(p, "wb") as f:
                f.write(b"audio")

        pls_content = (
            "[playlist]\n"
            "File1=01.mp3\n"
            "Title1=Queen - Bohemian Rhapsody\n"
            "Length1=354\n"
            f"File2={s2}\n"
            "Title2=Don't Stop Me Now\n"
            "Length2=210\n"
            "NumberOfEntries=2\n"
            "Version=2\n"
        )
        pls_file = os.path.join(self.root, "queen.pls")
        with open(pls_file, "w", encoding="utf-8") as f:
            f.write(pls_content)

        items = parse_pls(pls_file)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].artist, "Queen")
        self.assertEqual(items[0].title, "Bohemian Rhapsody")
        self.assertEqual(items[0].duration, 354.0)
        self.assertEqual(items[1].title, "Don't Stop Me Now")

    def test_parse_xspf(self) -> None:
        s1 = os.path.join(self.root, "track.mp3")
        with open(s1, "wb") as f:
            f.write(b"audio")

        xspf_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<playlist version="1" xmlns="http://xspf.org/ns/0/">
  <trackList>
    <track>
      <location>file://{s1}</location>
      <title>Test Title</title>
      <creator>Test Artist</creator>
      <album>Test Album</album>
      <duration>180000</duration>
    </track>
  </trackList>
</playlist>"""
        xspf_file = os.path.join(self.root, "test.xspf")
        with open(xspf_file, "w", encoding="utf-8") as f:
            f.write(xspf_content)

        items = parse_xspf(xspf_file)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Test Title")
        self.assertEqual(items[0].artist, "Test Artist")
        self.assertEqual(items[0].album, "Test Album")
        self.assertEqual(items[0].duration, 180.0)

    def test_parse_playlist_cycle_detection(self) -> None:
        p1 = os.path.join(self.root, "p1.m3u")
        p2 = os.path.join(self.root, "p2.m3u")
        with open(p1, "w", encoding="utf-8") as f:
            f.write("p2.m3u\n")
        with open(p2, "w", encoding="utf-8") as f:
            f.write("p1.m3u\n")

        items = parse_playlist_file(p1)
        self.assertEqual(items, [])


class TestPlaylistManagerWithPlaylistsAndCue(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = self.temp_dir.name

        self.audio = os.path.join(self.root, "cdimage.flac")
        with open(self.audio, "wb") as f:
            f.write(b"audio")

        self.cue_file = os.path.join(self.root, "cdimage.cue")
        with open(self.cue_file, "w", encoding="utf-8") as f:
            f.write(
                'PERFORMER "Artist"\n'
                'TITLE "Album"\n'
                'FILE "cdimage.flac" WAVE\n'
                '  TRACK 01 AUDIO\n'
                '    TITLE "Track 1"\n'
                '    INDEX 01 00:00:00\n'
                '  TRACK 02 AUDIO\n'
                '    TITLE "Track 2"\n'
                '    INDEX 01 01:30:00\n'
                '  TRACK 03 AUDIO\n'
                '    TITLE "Track 3"\n'
                '    INDEX 01 03:00:00\n'
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_load_cue_file_directly(self) -> None:
        pm = PlaylistManager()
        first_track = pm.load_file_or_folder(self.cue_file)
        self.assertIsInstance(first_track, TrackInfo)
        self.assertEqual(first_track.title, "Track 1")
        self.assertEqual(len(pm), 3)

        t2 = pm.get_next()
        self.assertIsInstance(t2, TrackInfo)
        self.assertEqual(t2.title, "Track 2")
        self.assertEqual(t2.start_time, 90.0)

        t3 = pm.get_next()
        self.assertIsInstance(t3, TrackInfo)
        self.assertEqual(t3.title, "Track 3")
        self.assertEqual(t3.start_time, 180.0)

    def test_folder_scan_with_cue_sheet(self) -> None:
        standalone = os.path.join(self.root, "bonus.mp3")
        with open(standalone, "wb") as f:
            f.write(b"audio")

        items = scan_audio_files_single_folder(self.root)
        self.assertEqual(len(items), 4)  # 3 CUE tracks + 1 bonus.mp3 (cdimage.flac is deduplicated)
        paths = [p.path if isinstance(p, TrackInfo) else p for p in items]
        self.assertIn(standalone, paths)
        cue_titles = [p.title for p in items if isinstance(p, TrackInfo)]
        self.assertIn("Track 1", cue_titles)
        self.assertIn("Track 2", cue_titles)
        self.assertIn("Track 3", cue_titles)

    def test_cue_noshuffle_setting(self) -> None:
        # When cue_noshuffle = 1 and remember_toggles has shuffle = True:
        with patch("playlist.load_toggles_state", return_value=(True, "None")), \
             patch("playlist.get_config", return_value=Config(remember_toggles=1, cue_noshuffle=1)):
            pm = PlaylistManager()
            pm.load_file_or_folder(self.cue_file)
            self.assertFalse(pm.shuffle)
            self.assertEqual(pm.current_index, 0)

        # When cue_noshuffle = 0 and remember_toggles has shuffle = True:
        with patch("playlist.load_toggles_state", return_value=(True, "None")), \
             patch("playlist.get_config", return_value=Config(remember_toggles=1, cue_noshuffle=0)):
            pm = PlaylistManager()
            pm.load_file_or_folder(self.cue_file)
            self.assertTrue(pm.shuffle)

    def test_cue_order_setting(self) -> None:
        # Create a CUE file where track titles are in non-alphabetical order
        cue_path = os.path.join(self.root, "unordered.cue")
        with open(cue_path, "w", encoding="utf-8") as f:
            f.write(
                'TITLE "Album"\n'
                'FILE "cdimage.flac" WAVE\n'
                '  TRACK 01 AUDIO\n'
                '    TITLE "Zebra"\n'
                '    INDEX 01 00:00:00\n'
                '  TRACK 02 AUDIO\n'
                '    TITLE "Alpha"\n'
                '    INDEX 01 01:00:00\n'
                '  TRACK 03 AUDIO\n'
                '    TITLE "Beta"\n'
                '    INDEX 01 02:00:00\n'
            )

        # When cue_order = 0 (default), sorting is NOT applied to cue files
        with patch("playlist.get_config", return_value=Config(cue_order=0, sort_method=1)):
            pm = PlaylistManager()
            pm.load_file_or_folder(cue_path)
            titles = [t.title for t in pm.items]
            self.assertEqual(titles, ["Zebra", "Alpha", "Beta"])

        # When cue_order = 1, sort_method (1 = sort by title) IS applied to cue files
        with patch("playlist.get_config", return_value=Config(cue_order=1, sort_method=1)):
            pm = PlaylistManager()
            pm.load_file_or_folder(cue_path)
            titles = [t.title for t in pm.items]
            self.assertEqual(titles, ["Alpha", "Beta", "Zebra"])


class TestPlayerEngineCuePlayback(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PlayerEngine()

    def tearDown(self) -> None:
        self.engine.close()

    def test_player_engine_load_cue_track_info(self) -> None:
        t = TrackInfo(
            path="/tmp/fake_cue.flac",
            title="CUE Track 2",
            artist="Artist",
            album="Album",
            start_time=90.0,
            end_time=210.0,
            duration=120.0,
        )

        with patch("player.probe_track") as mock_probe:
            mock_probe.return_value = TrackInfo(
                path="/tmp/fake_cue.flac",
                title="Full Album",
                artist="Full Artist",
                duration=3600.0,
                sample_rate=44100,
                channels=2,
                sample_fmt="s16",
                ffmpeg_fmt="s16le",
                pwcat_fmt="s16",
            )
            loaded = self.engine.load(t)
            self.assertEqual(loaded.title, "CUE Track 2")
            self.assertEqual(loaded.duration, 120.0)
            self.assertEqual(loaded.start_time, 90.0)
            self.assertEqual(loaded.end_time, 210.0)

    def test_ffmpeg_cue_proc_args(self) -> None:
        t = TrackInfo(
            path="/tmp/fake.flac",
            title="Track",
            start_time=60.0,
            end_time=180.0,
            duration=120.0,
        )
        with patch("subprocess.Popen") as mock_popen, patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            mock_popen.return_value.poll.return_value = None
            self.engine._spawn_ffmpeg_proc(t, seek_pos=10.0)
            cmd = mock_popen.call_args[0][0]
            self.assertIn("-ss", cmd)
            self.assertEqual(cmd[cmd.index("-ss") + 1], "70.000000")  # 60.0 + 10.0
            self.assertIn("-t", cmd)
            self.assertEqual(cmd[cmd.index("-t") + 1], "110.000000")  # 120.0 - 10.0


class TestExtractCoverFolderFallback(unittest.TestCase):
    def test_folder_cover_art_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "song.flac")
            with open(audio_path, "wb") as f:
                f.write(b"audio")

            cover_path = os.path.join(tmpdir, "cover.jpg")
            fake_cover_data = b"fake_jpeg_image_data"
            with open(cover_path, "wb") as f:
                f.write(fake_cover_data)

            with patch("subprocess.run") as mock_run:
                mock_res = MagicMock()
                mock_res.returncode = 1
                mock_res.stdout = None
                mock_run.return_value = mock_res

                art = extract_cover(audio_path)
                self.assertEqual(art, fake_cover_data)


class TestFragileCueAndPlaylistEdgeCases(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = self.temp_dir.name

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cue_lowercase_and_heavy_whitespace(self) -> None:
        audio = os.path.join(self.root, "song.flac")
        with open(audio, "wb") as f:
            f.write(b"audio")

        cue_content = (
            '   performer    "Lower Artist"   \n'
            '   title   "Lower Title"   \n'
            '   file   "song.flac"   wave   \n'
            '     track   01   audio   \n'
            '       title   "Sub Track 1"   \n'
            '       index   01   00:00:00   \n'
            '     track   02   audio   \n'
            '       title   "Sub Track 2"   \n'
            '       index   01   01:10:30   \n'
        )
        cue_path = os.path.join(self.root, "lower.cue")
        with open(cue_path, "w", encoding="utf-8") as f:
            f.write(cue_content)

        tracks = parse_cue(cue_path)
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].title, "Sub Track 1")
        self.assertEqual(tracks[0].artist, "Lower Artist")
        self.assertEqual(tracks[0].album, "Lower Title")
        self.assertEqual(tracks[1].title, "Sub Track 2")
        self.assertAlmostEqual(tracks[1].start_time, 70.4, places=2)

    def test_cue_pregap_only_and_pregap_command(self) -> None:
        audio = os.path.join(self.root, "album.flac")
        with open(audio, "wb") as f:
            f.write(b"audio")

        cue_content = (
            'FILE "album.flac" WAVE\n'
            '  TRACK 01 AUDIO\n'
            '    TITLE "Track 1"\n'
            '    INDEX 00 00:00:00\n'  # No INDEX 01
            '  TRACK 02 AUDIO\n'
            '    TITLE "Track 2"\n'
            '    PREGAP 00:02:00\n'
            '    INDEX 01 01:00:00\n'
        )
        cue_path = os.path.join(self.root, "pregap.cue")
        with open(cue_path, "w", encoding="utf-8") as f:
            f.write(cue_content)

        tracks = parse_cue(cue_path)
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].start_time, 0.0)
        self.assertEqual(tracks[0].end_time, 58.0)  # 01:00:00 (60s) - PREGAP 2s = 58s

    def test_cue_stem_and_single_audio_fallback(self) -> None:
        # CUE references 'phantom.wav', but folder contains 'album.flac'
        flac_audio = os.path.join(self.root, "album.flac")
        with open(flac_audio, "wb") as f:
            f.write(b"audio")

        cue_content = (
            'FILE "phantom.wav" WAVE\n'
            '  TRACK 01 AUDIO\n'
            '    TITLE "Fallback Track"\n'
            '    INDEX 01 00:00:00\n'
        )
        cue_path = os.path.join(self.root, "album.cue")
        with open(cue_path, "w", encoding="utf-8") as f:
            f.write(cue_content)

        tracks = parse_cue(cue_path)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].path, flac_audio)

    def test_cue_utf8_bom_and_empty_files(self) -> None:
        # 1. 0-byte file
        empty_cue = os.path.join(self.root, "empty.cue")
        with open(empty_cue, "wb") as f:
            pass
        self.assertEqual(parse_cue(empty_cue), [])

        # 2. Non-existent file
        self.assertEqual(parse_cue("/tmp/non_existent_file.cue"), [])

        # 3. UTF-8 BOM file
        bom_cue = os.path.join(self.root, "bom.cue")
        audio = os.path.join(self.root, "bom.flac")
        with open(audio, "wb") as f:
            f.write(b"audio")

        content = (
            '\ufeffTITLE "BOM Album"\n'
            'FILE "bom.flac" WAVE\n'
            '  TRACK 01 AUDIO\n'
            '    TITLE "BOM Track"\n'
            '    INDEX 01 00:00:00\n'
        )
        with open(bom_cue, "w", encoding="utf-8-sig") as f:
            f.write(content)

        tracks = parse_cue(bom_cue)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].album, "BOM Album")
        self.assertEqual(tracks[0].title, "BOM Track")

    def test_m3u_stream_negative_duration_and_spaces(self) -> None:
        audio = os.path.join(self.root, "my track with spaces.mp3")
        with open(audio, "wb") as f:
            f.write(b"audio")

        m3u_content = (
            "#EXTM3U\n"
            "#EXTINF:-1,Web Radio - Live Stream\n"
            "http://stream.example.com/live.mp3\n"
            "#EXTINF:180.75,My Artist - Space Track\n"
            "my%20track%20with%20spaces.mp3\n"
        )
        m3u_path = os.path.join(self.root, "stream.m3u")
        with open(m3u_path, "w", encoding="utf-8") as f:
            f.write(m3u_content)

        items = parse_m3u(m3u_path)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].path, "http://stream.example.com/live.mp3")
        self.assertEqual(items[0].artist, "Web Radio")
        self.assertEqual(items[0].title, "Live Stream")
        self.assertEqual(items[0].duration, 0.0)

        self.assertEqual(items[1].path, audio)
        self.assertEqual(items[1].artist, "My Artist")
        self.assertEqual(items[1].title, "Space Track")
        self.assertEqual(items[1].duration, 180.75)

    def test_xspf_malformed_xml_and_no_duration(self) -> None:
        # Malformed XML
        bad_xml = os.path.join(self.root, "bad.xspf")
        with open(bad_xml, "w", encoding="utf-8") as f:
            f.write("<playlist><trackList><track><location>unclosed")
        self.assertEqual(parse_xspf(bad_xml), [])

        # Valid XML without duration or artist
        audio = os.path.join(self.root, "simple.mp3")
        with open(audio, "wb") as f:
            f.write(b"audio")

        valid_xml = os.path.join(self.root, "valid.xspf")
        with open(valid_xml, "w", encoding="utf-8") as f:
            f.write(f"<playlist><trackList><track><location>{audio}</location></track></trackList></playlist>")
        items = parse_xspf(valid_xml)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0], audio)


class TestFragilePlaylistManagerAndScanning(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = self.temp_dir.name

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_scan_multiple_cues_and_ignore_hidden(self) -> None:
        disc1_audio = os.path.join(self.root, "disc1.flac")
        disc2_audio = os.path.join(self.root, "disc2.flac")
        hidden_audio = os.path.join(self.root, ".hidden.flac")
        text_file = os.path.join(self.root, "notes.txt")

        for p in (disc1_audio, disc2_audio, hidden_audio, text_file):
            with open(p, "wb") as f:
                f.write(b"data")

        with open(os.path.join(self.root, "disc1.cue"), "w", encoding="utf-8") as f:
            f.write('FILE "disc1.flac" WAVE\n  TRACK 01 AUDIO\n    TITLE "D1T1"\n    INDEX 01 00:00:00\n')

        with open(os.path.join(self.root, "disc2.cue"), "w", encoding="utf-8") as f:
            f.write('FILE "disc2.flac" WAVE\n  TRACK 01 AUDIO\n    TITLE "D2T1"\n    INDEX 01 00:00:00\n')

        items = scan_audio_files_single_folder(self.root)
        self.assertEqual(len(items), 2)
        titles = [t.title for t in items if isinstance(t, TrackInfo)]
        self.assertIn("D1T1", titles)
        self.assertIn("D2T1", titles)

    def test_empty_and_single_item_playlist_manager_boundaries(self) -> None:
        pm = PlaylistManager()
        self.assertEqual(len(pm), 0)
        self.assertIsNone(pm.current_track())
        self.assertIsNone(pm.get_next())
        self.assertIsNone(pm.get_previous())
        self.assertFalse(pm.can_go_next())
        self.assertFalse(pm.can_go_previous())

        # Load non-existent file
        pm.load_file_or_folder(os.path.join(self.root, "missing.cue"))
        self.assertEqual(len(pm), 0)

        # Single item playlist
        s1 = os.path.join(self.root, "single.mp3")
        with open(s1, "wb") as f:
            f.write(b"audio")

        pm._items = [s1]
        pm._current_index = 0
        self.assertFalse(pm.can_go_next())
        self.assertFalse(pm.can_go_previous())
        self.assertIsNone(pm.get_next())
        self.assertIsNone(pm.get_previous())

        # Loop Track allows repeating
        pm.loop_status = "Track"
        self.assertTrue(pm.can_go_next())
        self.assertEqual(pm.get_next(auto_advance=True), s1)

    def test_smart_shuffle_edge_cases(self) -> None:
        from playlist import _generate_smart_shuffle_indices
        # 0, 1, 2 items
        self.assertEqual(_generate_smart_shuffle_indices([], -1), [])
        self.assertEqual(_generate_smart_shuffle_indices(["a.mp3"], 0), [0])
        self.assertEqual(set(_generate_smart_shuffle_indices(["a.mp3", "b.mp3"], 0)), {0, 1})

        # Smart shuffle with TrackInfo objects without probing metadata
        t1 = TrackInfo(path="/tmp/1.flac", artist="ArtistA", title="T1")
        t2 = TrackInfo(path="/tmp/2.flac", artist="ArtistA", title="T2")
        t3 = TrackInfo(path="/tmp/3.flac", artist="ArtistB", title="T3")
        t4 = TrackInfo(path="/tmp/4.flac", artist="ArtistC", title="T4")

        indices = _generate_smart_shuffle_indices([t1, t2, t3, t4], 0)
        self.assertEqual(len(indices), 4)
        self.assertEqual(indices[0], 0)
        self.assertEqual(set(indices), {0, 1, 2, 3})


class TestFragilePlayerEngineAndPlaybackPaths(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PlayerEngine()

    def tearDown(self) -> None:
        self.engine.close()

    def test_subtrack_clamping_and_seek_remaining(self) -> None:
        t = TrackInfo(
            path="/tmp/song.flac",
            title="SubTrack",
            start_time=60.0,
            end_time=180.0,
            duration=120.0,
        )

        with patch("subprocess.Popen") as mock_popen, patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            mock_popen.return_value.poll.return_value = None

            # Seek position at 30.0 -> -ss 90.0, -t 90.0
            self.engine._spawn_ffmpeg_proc(t, seek_pos=30.0)
            cmd = mock_popen.call_args[0][0]
            self.assertEqual(cmd[cmd.index("-ss") + 1], "90.000000")
            self.assertEqual(cmd[cmd.index("-t") + 1], "90.000000")

    def test_extract_cover_corrupt_stream_and_case_insensitive_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "song.flac")
            with open(audio_path, "wb") as f:
                f.write(b"audio")

            # Create upper-case Cover.PNG
            cover_path = os.path.join(tmpdir, "Cover.PNG")
            fake_cover_data = b"\x89PNG\r\n\x1a\nfake_png"
            with open(cover_path, "wb") as f:
                f.write(fake_cover_data)

            # Subprocess raises OSError
            with patch("subprocess.run", side_effect=OSError("ffmpeg failed")):
                art = extract_cover(audio_path)
                self.assertEqual(art, fake_cover_data)

    def test_fmt_time_extreme_values(self) -> None:
        self.assertEqual(fmt_time(-10.0), "0:00")
        self.assertEqual(fmt_time(0.0), "0:00")
        self.assertEqual(fmt_time(59.9), "0:59")
        self.assertEqual(fmt_time(3600.0), "1:00:00")
        self.assertEqual(fmt_time(36000.0), "10:00:00")


class TestFragileMprisService(unittest.TestCase):
    def test_mpris_get_set_properties_and_seek(self) -> None:
        from mpris import _DBUS_OK, _PLAYER, _MPRIS
        if not _DBUS_OK:
            return
        engine = PlayerEngine()
        playlist = PlaylistManager()
        try:
            with patch("dbus.SessionBus"), \
                 patch("dbus.service.BusName"), \
                 patch("dbus.service.Object.__init__", return_value=None):
                service = MprisService(engine, playlist)
                service.PropertiesChanged = MagicMock()

                # Volume Set and Get
                service.Set(_PLAYER, "Volume", 0.75)
                self.assertAlmostEqual(engine.volume, 0.75, places=2)
                all_props = service.GetAll(_PLAYER)
                self.assertEqual(all_props["PlaybackStatus"], "Stopped")

                # Shuffle Set
                service.Set(_PLAYER, "Shuffle", True)
                self.assertTrue(playlist.shuffle)

                # LoopStatus Set
                service.Set(_PLAYER, "LoopStatus", "Track")
                self.assertEqual(playlist.loop_status, "Track")

                # MPRIS identity
                mpris_props = service.GetAll(_MPRIS)
                self.assertEqual(mpris_props["Identity"], APP_NAME)
        finally:
            engine.close()
            from config import save_volume_state
            save_volume_state(1.0)


class TestMprisService(unittest.TestCase):
    def test_track_id_formatting(self) -> None:
        self.assertEqual(str(_track_id(0)), f"{TRACK_OBJ_PATH}/0")
        self.assertEqual(str(_track_id(42)), f"{TRACK_OBJ_PATH}/42")

    def test_mpris_metadata_cue_track(self) -> None:
        from mpris import _DBUS_OK
        if not _DBUS_OK:
            return
        engine = PlayerEngine()
        try:
            playlist = PlaylistManager()
            with patch("dbus.SessionBus"), \
                 patch("dbus.service.BusName"), \
                 patch("dbus.service.Object.__init__", return_value=None):
                service = MprisService(engine, playlist)

                t = TrackInfo(
                    path="/tmp/audio.flac",
                    title="Sub Track",
                    artist="Artist",
                    album="Album",
                    duration=150.0,
                    track_number=3,
                    disc_number=1,
                    cue_path="/tmp/audio.cue",
                )
                meta = service._metadata_for(t, index=2)
                self.assertEqual(meta["xesam:title"], "Sub Track")
                self.assertEqual(meta["xesam:artist"][0], "Artist")
                self.assertEqual(meta["xesam:album"], "Album")
                self.assertEqual(meta["xesam:trackNumber"], 3)
                self.assertEqual(meta["xesam:discNumber"], 1)
                self.assertEqual(meta["xesam:url"], "file:///tmp/audio.cue")
        finally:
            engine.close()


class TestFragileAutoCloseManager(unittest.TestCase):
    def test_autoclose_paused_and_cancel_on_play(self) -> None:
        from main import AutoCloseManager
        callback_called = []
        ac = AutoCloseManager(timeout_minutes=15, on_timeout_callback=lambda: callback_called.append(True))
        
        # Changing to paused starts timer
        ac.on_state_changed("paused")
        self.assertIsNotNone(ac._timer)

        # Changing to playing cancels timer
        ac.on_state_changed("playing")
        self.assertIsNone(ac._timer)

        # Trigger timeout directly
        with patch("main.send_notification") as mock_notify:
            ac._trigger_timeout()
            self.assertTrue(callback_called[0])
            mock_notify.assert_called_once()

    def test_autoclose_disabled_when_zero(self) -> None:
        from main import AutoCloseManager
        ac = AutoCloseManager(timeout_minutes=0, on_timeout_callback=lambda: None)
        ac.on_state_changed("paused")
        self.assertIsNone(ac._timer)


class TestFragileReplayGainAndProbing(unittest.TestCase):
    def test_find_tag_float_variations(self) -> None:
        from player import _find_tag_float

        tags1 = {"replaygain_track_gain": "+3.45 dB"}
        self.assertAlmostEqual(_find_tag_float(tags1, ("replaygain_track_gain",)), 3.45)

        tags2 = {"REPLAYGAIN_TRACK_GAIN": "-6.20dB"}
        self.assertAlmostEqual(_find_tag_float(tags2, ("replaygain_track_gain",)), -6.20)

        tags3 = {"r128_track_gain": "-2.5"}
        self.assertAlmostEqual(_find_tag_float(tags3, ("replaygain_track_gain",), ("r128_track_gain",)), -2.5)

        tags4 = {"replaygain_track_gain": "corrupt_text"}
        self.assertIsNone(_find_tag_float(tags4, ("replaygain_track_gain",)))

        self.assertIsNone(_find_tag_float({}, ("replaygain_track_gain",)))

    def test_normalize_peak_variations(self) -> None:
        from player import _normalize_peak

        self.assertIsNone(_normalize_peak(None, "s16"))
        self.assertAlmostEqual(_normalize_peak(32768.0, "s16"), 1.0)
        self.assertAlmostEqual(_normalize_peak(1.0, "s16"), 1.0)
        self.assertAlmostEqual(_normalize_peak(8388608.0, "s24"), 1.0)
        self.assertAlmostEqual(_normalize_peak(2147483648.0, "s32"), 1.0)
        self.assertAlmostEqual(_normalize_peak(0.985, "flt"), 0.985)


class TestFragileConfigAndCLI(unittest.TestCase):
    def test_corrupt_ini_file_handling(self) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".ini") as f:
            f.write("[smolplayer\ncorrupt! syntax without closing bracket\n")
            tmp_path = f.name

        try:
            with patch("config._CONFIG_FILE", tmp_path):
                cfg = get_config()
                self.assertEqual(cfg.replay_gain, 0)
                self.assertEqual(cfg.shuffle_algo, 0)
                self.assertEqual(cfg.sort_method, 0)
                self.assertEqual(cfg.lossy_bps, 0)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_invalid_type_config_values(self) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".ini") as f:
            f.write("[smolplayer]\n"
                    "shuffle_algo = invalid_string\n"
                    "lossy_bps = not_an_int\n"
                    "sort_method = invalid\n")
            tmp_path = f.name

        try:
            with patch("config._CONFIG_FILE", tmp_path):
                cfg = get_config()
                self.assertEqual(cfg.shuffle_algo, 0)
                self.assertEqual(cfg.lossy_bps, 0)
                self.assertEqual(cfg.sort_method, 0)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


class TestDacCapabilitiesAndBitPerfectFallback(unittest.TestCase):
    def test_parse_proc_asound_stream_usb_dac(self) -> None:
        sample_stream0 = (
            "Playback:\n"
            "  Status: Stop\n"
            "  Interface 1\n"
            "    Altset 1\n"
            "      Format: S16_LE\n"
            "      Channels: 2\n"
            "      Endpoint: 0x01 (1 OUT) (ASYNC)\n"
            "      Rates: 44100, 48000, 88200, 96000, 176400, 192000\n"
            "      Bits: 16\n"
            "    Altset 2\n"
            "      Format: S32_LE\n"
            "      Channels: 2\n"
            "      Endpoint: 0x01 (1 OUT) (ASYNC)\n"
            "      Rates: 44100 - 768000 (continuous)\n"
            "      Bits: 24\n"
            "Capture:\n"
            "  Status: Stop\n"
            "  Interface 2\n"
            "    Altset 1\n"
            "      Format: S16_LE\n"
            "      Channels: 1\n"
            "      Rates: 48000\n"
        )
        caps = parse_proc_asound_stream(sample_stream0)
        self.assertIn("S16_LE", caps.formats)
        self.assertIn("S32_LE", caps.formats)
        self.assertEqual(caps.max_channels, 2)
        self.assertIn(44100, caps.sample_rates)
        self.assertIn(192000, caps.sample_rates)
        self.assertEqual(caps.min_rate, 44100)
        self.assertEqual(caps.max_rate, 768000)

    def test_parse_proc_asound_codec_hda(self) -> None:
        sample_codec = (
            "Codec: Realtek ALC236\n"
            "PCM:\n"
            "  rates [0x560]: 44100 48000 96000 192000\n"
            "  bits [0xe]: 16 20 24\n"
            "  formats [0x1]: PCM\n"
        )
        caps = parse_proc_asound_codec(sample_codec)
        self.assertIn("S16_LE", caps.formats)
        self.assertIn("S32_LE", caps.formats)
        self.assertIn(44100, caps.sample_rates)
        self.assertIn(192000, caps.sample_rates)
        self.assertNotIn(88200, caps.sample_rates)

    def test_check_dac_supports_track_sample_rate(self) -> None:
        caps = DacCapabilities(
            formats={"S16_LE", "S32_LE"},
            sample_rates={44100, 48000, 96000},
            max_channels=2,
        )
        t_supported = TrackInfo(path="/tmp/test.flac", sample_rate=96000, channels=2, pwcat_fmt="s32")
        t_unsupported = TrackInfo(path="/tmp/test.flac", sample_rate=192000, channels=2, pwcat_fmt="s32")

        with patch("player.get_dac_hardware_capabilities", return_value=caps):
            ok, reason = check_dac_supports_track("plughw:0,0", t_supported)
            self.assertTrue(ok)
            self.assertIsNone(reason)

            ok, reason = check_dac_supports_track("plughw:0,0", t_unsupported)
            self.assertFalse(ok)
            self.assertIsNotNone(reason)
            self.assertIn("192000Hz", reason)

    def test_check_dac_supports_track_format(self) -> None:
        caps = DacCapabilities(
            formats={"S16_LE", "S32_LE"},
            sample_rates={44100, 48000},
            max_channels=2,
        )
        t_int = TrackInfo(path="/tmp/test.flac", sample_rate=44100, channels=2, pwcat_fmt="s16")
        t_float = TrackInfo(path="/tmp/test.wav", sample_rate=44100, channels=2, pwcat_fmt="f32")

        with patch("player.get_dac_hardware_capabilities", return_value=caps):
            ok, reason = check_dac_supports_track("plughw:0,0", t_int)
            self.assertTrue(ok)

            ok, reason = check_dac_supports_track("plughw:0,0", t_float)
            self.assertFalse(ok)
            self.assertIn("32-bit floating point", reason)

    def test_check_dac_supports_track_channels(self) -> None:
        caps = DacCapabilities(
            formats={"S16_LE", "S32_LE"},
            sample_rates={44100, 48000},
            max_channels=2,
        )
        t_stereo = TrackInfo(path="/tmp/test.flac", sample_rate=44100, channels=2, pwcat_fmt="s16")
        t_surround = TrackInfo(path="/tmp/test.flac", sample_rate=44100, channels=6, pwcat_fmt="s16")

        with patch("player.get_dac_hardware_capabilities", return_value=caps):
            ok, reason = check_dac_supports_track("plughw:0,0", t_stereo)
            self.assertTrue(ok)

            ok, reason = check_dac_supports_track("plughw:0,0", t_surround)
            self.assertFalse(ok)
            self.assertIn("6 channels", reason)

    def test_launch_pipeline_unsupported_rate_triggers_early_fallback(self) -> None:
        engine = PlayerEngine()
        try:
            engine.bit_perfect = 1
            t = TrackInfo(path="/tmp/test.flac", sample_rate=352800, channels=2, pwcat_fmt="s32")
            engine._track = t
            caps = DacCapabilities(
                formats={"S16_LE", "S32_LE"},
                sample_rates={44100, 48000, 96000, 192000},
                max_channels=2,
            )

            with patch("player.get_dac_hardware_capabilities", return_value=caps), \
                 patch("player.send_error_notification") as mock_notif, \
                 patch("subprocess.Popen") as mock_popen, \
                 patch("shutil.which", return_value="/usr/bin/ffmpeg"):
                mock_popen.return_value.poll.return_value = None
                success = engine._launch_pipeline()
                self.assertTrue(success)
                mock_notif.assert_called_once()
                self.assertIn("352800Hz", mock_notif.call_args[0][1])
        finally:
            engine.close()

    def test_launch_pipeline_aplay_startup_stderr_capture(self) -> None:
        engine = PlayerEngine()
        try:
            engine.bit_perfect = 1
            t = TrackInfo(path="/tmp/test.flac", sample_rate=44100, channels=2, pwcat_fmt="s16")
            engine._track = t

            mock_aplay_proc = MagicMock()
            mock_aplay_proc.poll.return_value = 1
            mock_aplay_proc.returncode = 1
            mock_aplay_proc.stderr.read.return_value = b"aplay: audio open error: Device or resource busy"

            mock_pwcat_proc = MagicMock()
            mock_pwcat_proc.poll.return_value = None

            def fake_popen(cmd, **kwargs):
                if cmd[0] == "aplay":
                    return mock_aplay_proc
                return mock_pwcat_proc

            with patch("player.get_dac_hardware_capabilities", return_value=None), \
                 patch("player.send_error_notification") as mock_notif, \
                 patch("subprocess.Popen", side_effect=fake_popen), \
                 patch("shutil.which", return_value="/usr/bin/ffmpeg"):
                success = engine._launch_pipeline()
                self.assertTrue(success)
                mock_notif.assert_called_once()
                self.assertIn("Device or resource busy", mock_notif.call_args[0][1])
        finally:
            engine.close()


class TestVersion(unittest.TestCase):
    def test_app_version(self) -> None:
        from constants import APP_VERSION
        self.assertEqual(APP_VERSION, "0.1.0")


if __name__ == "__main__":
    unittest.main()

