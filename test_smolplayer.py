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
from player import (
    PlayerEngine, TrackInfo, STATE_PLAYING, STATE_PAUSED, STATE_STOPPED,
    get_system_sink_info,
)
from tray import TrayService
from mpris import _track_id
from config import Config, get_config, open_config_file, get_config_file_path
from constants import APP_NAME, TRACK_OBJ_PATH


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
                with open(tmp_path, "r", encoding="utf-8") as f_read:
                    content = f_read.read()
                    self.assertIn("replaygain_preamp = 0", content)
                    self.assertIn("replaygain_default_preamp = 0", content)
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

    def test_open_folder_with_shuffle_enabled_plays_random_track(self) -> None:
        with patch("playlist.get_config", return_value=Config(remember_toggles=1)), \
             patch("playlist.load_toggles_state", return_value=(True, "None")), \
             patch("random.randint", return_value=2):
            pm = PlaylistManager()
            first_track = pm.load_file_or_folder(self.root)
            self.assertEqual(first_track, self.f3)
            self.assertEqual(pm.current_index, 2)
            self.assertEqual(pm._shuffled_indices[0], 2)


class TestPlayerEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PlayerEngine()

    def tearDown(self) -> None:
        self.engine.close()

    def test_engine_initial_state(self) -> None:
        with patch.object(self.engine, "sync_system_volume"):
            self.assertEqual(self.engine.state, STATE_STOPPED)
            self.assertEqual(self.engine.volume, 1.0)
            self.assertIsNone(self.engine.track)


    def test_volume_clamping(self) -> None:
        self.engine.set_volume(1.5)
        self.assertLessEqual(self.engine.volume, 1.0)

        self.engine.set_volume(-0.5)
        self.assertGreaterEqual(self.engine.volume, 0.0)

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
            self.assertIn("volume=-0.50dB", cmd)  # -3.50 + 3.0 = -0.50

        # Track Gain ON without RG info (uses default preamp)
        with patch("subprocess.Popen") as mock_popen, patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            mock_popen.return_value.poll.return_value = None
            proc = self.engine._spawn_ffmpeg_proc(t_no_gain, 0.0)
            self.assertIsNotNone(proc)
            cmd = mock_popen.call_args[0][0]
            self.assertIn("-af", cmd)
            self.assertIn("volume=-2.00dB", cmd)

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
            self.assertEqual(cmd[cmd.index("-f") + 1], "s16le")

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
            self.assertEqual(pwcat_call[pwcat_call.index("--format") + 1], "s16")
            self.assertIn("--rate", pwcat_call)
            self.assertEqual(pwcat_call[pwcat_call.index("--rate") + 1], "48000")

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
             patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = None
            proc = self.engine._spawn_gstreamer_proc(t, 0.0)
            self.assertIsNotNone(proc)
            cmd = mock_popen.call_args[0][0]
            self.assertEqual(cmd[0], sys.executable)

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


class TestTrayService(unittest.TestCase):
    def test_tray_tooltip_text(self) -> None:
        engine = PlayerEngine()
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

    def test_tray_menu_events(self) -> None:
        from tray import _DBUS_OK, _MenuObject
        if not _DBUS_OK:
            return
        engine = PlayerEngine()
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




class TestMprisService(unittest.TestCase):
    def test_track_id_formatting(self) -> None:
        self.assertEqual(str(_track_id(0)), f"{TRACK_OBJ_PATH}/0")
        self.assertEqual(str(_track_id(42)), f"{TRACK_OBJ_PATH}/42")


class TestVersion(unittest.TestCase):
    def test_app_version(self) -> None:
        from constants import APP_VERSION
        self.assertEqual(APP_VERSION, "0.1.0")


if __name__ == "__main__":
    unittest.main()
