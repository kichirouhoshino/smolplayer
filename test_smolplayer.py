"""
test_smolplayer.py – Unit and integration test suite for smolplayer.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from utils import (
    AUDIO_EXTS,
    fmt_time,
    natural_sort_key,
    normalize_file_path,
    write_cover_art,
)
from playlist import PlaylistManager, scan_audio_files_recursive
from player import PlayerEngine, TrackInfo, STATE_PLAYING, STATE_PAUSED, STATE_STOPPED
from tray import TrayService
from mpris import _track_id
from config import Config, get_config


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
        from config import save_toggles_state, load_toggles_state
        save_toggles_state(True, "Playlist")
        shuf, loop = load_toggles_state()
        self.assertTrue(shuf)
        self.assertEqual(loop, "Playlist")
        save_toggles_state(False, "None")

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
                    "decode_method = 1\n")
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
                self.assertFalse(cfg.tray_enabled)
                self.assertTrue(cfg.notifications_enabled)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


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

    def test_shuffle_mode(self) -> None:
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


class TestPlayerEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PlayerEngine()

    def tearDown(self) -> None:
        self.engine.stop()

    def test_engine_initial_state(self) -> None:
        self.assertEqual(self.engine.state, STATE_STOPPED)
        self.assertEqual(self.engine.volume, 1.0)
        self.assertIsNone(self.engine.track)

    def test_volume_clamping(self) -> None:
        self.engine.set_volume(1.5)
        self.assertLessEqual(self.engine.volume, 1.0)

        self.engine.set_volume(-0.5)
        self.assertGreaterEqual(self.engine.volume, 0.0)

    def test_replaygain_cmd_args(self) -> None:
        t = TrackInfo(path="/tmp/fake.flac", duration=180.0)
        self.engine.replay_gain = 1
        with patch("subprocess.Popen") as mock_popen, patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            mock_popen.return_value.poll.return_value = None
            proc = self.engine._spawn_ffmpeg_proc(t, 0.0)
            self.assertIsNotNone(proc)
            cmd = mock_popen.call_args[0][0]
            self.assertIn("-af", cmd)
            self.assertIn("volume=replaygain=track", cmd)

        t_gain = TrackInfo(path="/tmp/fake.flac", duration=180.0, track_gain_db=-3.50)
        with patch("subprocess.Popen") as mock_popen, patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            mock_popen.return_value.poll.return_value = None
            proc = self.engine._spawn_ffmpeg_proc(t_gain, 0.0)
            self.assertIsNotNone(proc)
            cmd = mock_popen.call_args[0][0]
            self.assertIn("-af", cmd)
            self.assertIn("volume=-3.50dB", cmd)

    def test_decoder_fallback_when_ffmpeg_missing(self) -> None:
        t = TrackInfo(path="/tmp/fake.flac", duration=180.0)
        self.engine._track = t

        def fake_which(cmd: str):
            if cmd == "gst-launch-1.0":
                return "/usr/bin/gst-launch-1.0"
            return None

        with patch("shutil.which", side_effect=fake_which), \
             patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = None
            proc = self.engine._spawn_gstreamer_proc(t, 0.0)
            self.assertIsNotNone(proc)
            cmd = mock_popen.call_args[0][0]
            self.assertEqual(cmd[0], sys.executable)

    def test_gstreamer_seeking_cmd_args(self) -> None:
        t = TrackInfo(path="/tmp/fake.flac", duration=180.0)
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = None
            proc = self.engine._spawn_gstreamer_proc(t, 30.0)
            self.assertIsNotNone(proc)
            cmd = mock_popen.call_args[0][0]
            self.assertIn("p.seek_simple", cmd[2])
            self.assertIn("30.0 * 1e9", cmd[2])

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


class TestTrayService(unittest.TestCase):
    def test_tray_tooltip_text(self) -> None:
        engine = PlayerEngine()
        playlist = PlaylistManager()

        tray = TrayService(engine, playlist)
        self.assertEqual(tray.get_tooltip_text(), "smolplayer - Stopped")

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


class TestMprisService(unittest.TestCase):
    def test_track_id_formatting(self) -> None:
        self.assertEqual(str(_track_id(0)), "/org/smolplayer/Track/0")
        self.assertEqual(str(_track_id(42)), "/org/smolplayer/Track/42")


if __name__ == "__main__":
    unittest.main()
