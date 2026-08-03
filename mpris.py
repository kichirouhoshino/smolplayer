"""
mpris.py – MPRIS2 D-Bus service implementation for smolplayer.

Exports:
  org.mpris.MediaPlayer2
  org.mpris.MediaPlayer2.Player
"""

from __future__ import annotations

import ctypes
import threading
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import unquote, urlparse

from utils import send_notification

try:
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    _DBUS_OK = True
except ImportError:
    _DBUS_OK = False

if TYPE_CHECKING:
    from player import PlayerEngine, TrackInfo
    from playlist import PlaylistManager

_BUS_NAME = "org.mpris.MediaPlayer2.smolplayer"
_OBJ_PATH = "/org/mpris/MediaPlayer2"
_MPRIS    = "org.mpris.MediaPlayer2"
_PLAYER   = "org.mpris.MediaPlayer2.Player"
_PROPS    = "org.freedesktop.DBus.Properties"


if _DBUS_OK:

    def _us(secs: float) -> dbus.Int64:
        return dbus.Int64(max(0, int(secs * 1_000_000)))

    def _track_id(index: int) -> dbus.ObjectPath:
        if index < 0:
            return dbus.ObjectPath("/org/smolplayer/Track/None")
        return dbus.ObjectPath(f"/org/smolplayer/Track/{index}")

    class MprisService(dbus.service.Object):
        """Full MPRIS2 implementation backed by PlayerEngine and PlaylistManager."""

        def __init__(
            self,
            engine: PlayerEngine,
            playlist: PlaylistManager,
            on_quit: Optional[Any] = None,
            on_open_uri: Optional[Any] = None,
        ) -> None:
            DBusGMainLoop(set_as_default=True)
            self._engine = engine
            self._playlist = playlist
            self._on_quit = on_quit
            self._on_open_uri = on_open_uri
            self._bus = dbus.SessionBus()
            self._bus_name = dbus.service.BusName(
                _BUS_NAME, self._bus, do_not_queue=True
            )
            super().__init__(self._bus, _OBJ_PATH)

        # ---------------------------------------------------------------
        # Outbound notifications
        # ---------------------------------------------------------------

        def notify_state(self, state: str) -> None:
            changed = {
                "PlaybackStatus": self._playback_status(state),
                "CanPause": dbus.Boolean(state == "playing"),
                "CanSeek": dbus.Boolean(state != "stopped"),
            }
            self.PropertiesChanged(_PLAYER, changed, [])

        def notify_track(self, info: Optional[TrackInfo], index: int) -> None:
            self.PropertiesChanged(
                _PLAYER,
                {
                    "Metadata": self._metadata_for(info, index),
                    "CanGoNext": dbus.Boolean(self._playlist.can_go_next()),
                    "CanGoPrevious": dbus.Boolean(self._playlist.can_go_previous()),
                    "CanPlay": dbus.Boolean(True),
                    "CanSeek": dbus.Boolean(True),
                },
                [],
            )
            self.notify_seeked(self._engine.position)

        def notify_seeked(self, position_secs: float) -> None:
            self.Seeked(_us(position_secs))

        def notify_volume(self, volume: float) -> None:
            self.PropertiesChanged(_PLAYER, {"Volume": dbus.Double(volume)}, [])

        def notify_shuffle(self, shuffle: bool) -> None:
            self.PropertiesChanged(
                _PLAYER,
                {
                    "Shuffle": dbus.Boolean(shuffle),
                    "CanGoNext": dbus.Boolean(self._playlist.can_go_next()),
                    "CanGoPrevious": dbus.Boolean(self._playlist.can_go_previous()),
                },
                [],
            )

        def notify_loop(self, loop_status: str) -> None:
            self.PropertiesChanged(
                _PLAYER,
                {
                    "LoopStatus": dbus.String(loop_status),
                    "CanGoNext": dbus.Boolean(self._playlist.can_go_next()),
                    "CanGoPrevious": dbus.Boolean(self._playlist.can_go_previous()),
                },
                [],
            )

        @dbus.service.signal(_PROPS, signature="sa{sv}as")
        def PropertiesChanged(
            self,
            interface_name: str,
            changed_properties: dict,
            invalidated_properties: list,
        ) -> None:
            pass

        # ---------------------------------------------------------------
        # Internal helpers
        # ---------------------------------------------------------------

        def _playback_status(self, state: str) -> str:
            if state == "playing":
                return "Playing"
            if state == "paused":
                return "Paused"
            return "Stopped"

        def _metadata_for(self, info: Optional[TrackInfo], index: int) -> dbus.Dictionary:
            meta = dbus.Dictionary({}, signature="sv")
            meta["mpris:trackid"] = _track_id(index)
            if info is None:
                return meta

            meta["mpris:length"] = _us(info.duration)
            if info.title:
                meta["xesam:title"] = dbus.String(info.title)
            if info.artist:
                meta["xesam:artist"] = dbus.Array([info.artist], signature="s")
            if info.album:
                meta["xesam:album"] = dbus.String(info.album)

            if info.path.startswith("/"):
                meta["xesam:url"] = dbus.String(f"file://{info.path}")
            elif info.path.startswith("file://"):
                meta["xesam:url"] = dbus.String(info.path)

            if info.cover_url:
                meta["mpris:artUrl"] = dbus.String(info.cover_url)

            return meta

        # ---------------------------------------------------------------
        # DBus Properties
        # ---------------------------------------------------------------

        @dbus.service.method(_PROPS, in_signature="ss", out_signature="v")
        def Get(self, interface_name: str, property_name: str) -> Any:
            return self.GetAll(interface_name).get(property_name, "")

        @dbus.service.method(_PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface_name: str) -> dict:
            if interface_name == _MPRIS:
                return {
                    "CanQuit": dbus.Boolean(True),
                    "CanRaise": dbus.Boolean(False),
                    "HasTrackList": dbus.Boolean(False),
                    "Identity": dbus.String("smolplayer"),
                    "DesktopEntry": dbus.String("io.github.roddy.SmolPlayer"),
                    "SupportedUriSchemes": dbus.Array(["file"], signature="s"),
                    "SupportedMimeTypes": dbus.Array(
                        ["audio/mpeg", "audio/flac", "audio/ogg", "audio/wav", "audio/mp4"],
                        signature="s",
                    ),
                }
            if interface_name == _PLAYER:
                info = self._engine.track
                idx = self._playlist.current_index
                state = self._engine.state
                return {
                    "PlaybackStatus": dbus.String(self._playback_status(state)),
                    "LoopStatus": dbus.String(self._playlist.loop_status),
                    "Rate": dbus.Double(1.0),
                    "Shuffle": dbus.Boolean(self._playlist.shuffle),
                    "Metadata": self._metadata_for(info, idx),
                    "Volume": dbus.Double(self._engine.volume),
                    "Position": _us(self._engine.position),
                    "MinimumRate": dbus.Double(1.0),
                    "MaximumRate": dbus.Double(1.0),
                    "CanGoNext": dbus.Boolean(self._playlist.can_go_next()),
                    "CanGoPrevious": dbus.Boolean(self._playlist.can_go_previous()),
                    "CanPlay": dbus.Boolean(True),
                    "CanPause": dbus.Boolean(state == "playing"),
                    "CanSeek": dbus.Boolean(state != "stopped"),
                    "CanControl": dbus.Boolean(True),
                }
            raise dbus.exceptions.DBusException(
                f"Unknown interface {interface_name}",
                name="org.freedesktop.DBus.Error.UnknownInterface",
            )

        @dbus.service.method(_PROPS, in_signature="ssv", out_signature="")
        def Set(self, interface_name: str, property_name: str, value: Any) -> None:
            if interface_name != _PLAYER:
                return

            if property_name == "Volume":
                val = float(value)
                self._engine.set_volume(val)
                self.notify_volume(val)
            elif property_name == "Shuffle":
                shuf = bool(value)
                self._playlist.shuffle = shuf
                self.notify_shuffle(shuf)
            elif property_name == "LoopStatus":
                ls = str(value)
                if ls in ("None", "Track", "Playlist"):
                    self._playlist.loop_status = ls
                    self.notify_loop(ls)

        # ---------------------------------------------------------------
        # org.mpris.MediaPlayer2 interface
        # ---------------------------------------------------------------

        @dbus.service.method(_MPRIS, in_signature="", out_signature="")
        def Quit(self) -> None:
            self._engine.stop()
            if self._on_quit:
                self._on_quit()

        @dbus.service.method(_MPRIS, in_signature="", out_signature="")
        def Raise(self) -> None:
            pass

        # ---------------------------------------------------------------
        # org.mpris.MediaPlayer2.Player interface
        # ---------------------------------------------------------------

        @dbus.service.method(_PLAYER, in_signature="", out_signature="")
        def Next(self) -> None:
            import os
            attempts = 0
            max_attempts = len(self._playlist)
            while attempts < max_attempts:
                nxt = self._playlist.get_next()
                if not nxt:
                    break
                attempts += 1
                if os.path.isfile(nxt):
                    info = self._engine.load(nxt)
                    self._engine.play()
                    self.notify_track(info, self._playlist.current_index)
                    self._engine.fetch_cover_async(
                        callback=lambda url: self.notify_track(self._engine.track, self._playlist.current_index)
                    )
                    return
            self._engine.stop()

        @dbus.service.method(_PLAYER, in_signature="", out_signature="")
        def Previous(self) -> None:
            if self._engine.position > 3.0:
                self._engine.seek(0.0)
                self.notify_seeked(0.0)
                return

            import os
            attempts = 0
            max_attempts = len(self._playlist)
            while attempts < max_attempts:
                prev = self._playlist.get_previous()
                if not prev:
                    break
                attempts += 1
                if os.path.isfile(prev):
                    info = self._engine.load(prev)
                    self._engine.play()
                    self.notify_track(info, self._playlist.current_index)
                    self._engine.fetch_cover_async(
                        callback=lambda url: self.notify_track(self._engine.track, self._playlist.current_index)
                    )
                    return
            self._engine.stop()

        @dbus.service.method(_PLAYER, in_signature="", out_signature="")
        def Pause(self) -> None:
            self._engine.pause()
            # State notification flows through engine.on_state_change callback

        @dbus.service.method(_PLAYER, in_signature="", out_signature="")
        def PlayPause(self) -> None:
            if self._engine.state == "playing":
                self._engine.pause()
            elif self._engine.state == "paused":
                self._engine.resume()
            else:
                curr = self._playlist.current_track()
                if curr:
                    info = self._engine.load(curr)
                    self._engine.play()
                    self.notify_track(info, self._playlist.current_index)

        @dbus.service.method(_PLAYER, in_signature="", out_signature="")
        def Stop(self) -> None:
            self._engine.stop()
            # State notification flows through engine.on_state_change callback

        @dbus.service.method(_PLAYER, in_signature="", out_signature="")
        def Play(self) -> None:
            if self._engine.state == "paused":
                self._engine.resume()
            elif self._engine.state == "stopped":
                curr = self._playlist.current_track()
                if curr:
                    info = self._engine.load(curr)
                    self._engine.play()
                    self.notify_track(info, self._playlist.current_index)

        @dbus.service.method(_PLAYER, in_signature="x", out_signature="")
        def Seek(self, offset_us: dbus.Int64) -> None:
            new_pos = self._engine.position + (offset_us / 1_000_000.0)
            self._engine.seek(new_pos)
            self.notify_seeked(self._engine.position)

        @dbus.service.method(_PLAYER, in_signature="ox", out_signature="")
        def SetPosition(self, track_id: dbus.ObjectPath, position_us: dbus.Int64) -> None:
            curr_id = _track_id(self._playlist.current_index)
            if track_id == curr_id:
                new_pos = position_us / 1_000_000.0
                self._engine.seek(new_pos)
                self.notify_seeked(self._engine.position)

        @dbus.service.method(_PLAYER, in_signature="s", out_signature="")
        def OpenUri(self, uri: str) -> None:
            if self._on_open_uri:
                self._on_open_uri(uri)

        @dbus.service.signal(_PLAYER, signature="x")
        def Seeked(self, position_us: dbus.Int64) -> None:
            pass

else:
    class MprisService:  # type: ignore
        def __init__(self, *_a, **_kw) -> None: pass
        def notify_state(self, *_a) -> None: pass
        def notify_track(self, *_a) -> None: pass
        def notify_seeked(self, *_a) -> None: pass
        def notify_volume(self, *_a) -> None: pass
        def notify_shuffle(self, *_a) -> None: pass
        def notify_loop(self, *_a) -> None: pass


def forward_files_to_existing(paths: list[str]) -> bool:
    """Forward file paths to existing smolplayer instance if running."""
    if not _DBUS_OK:
        return False
    try:
        DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        if not bus.name_has_owner(_BUS_NAME):
            return False

        if not paths:
            send_notification(
                "smolplayer",
                "smolplayer is already running in the background! Open an audio file in your file manager to start playback.",
            )
            return True

        obj = bus.get_object(_BUS_NAME, _OBJ_PATH)
        player = dbus.Interface(obj, _PLAYER)
        for p in paths:
            if p.startswith("file://"):
                player.OpenUri(p)
            else:
                player.OpenUri(f"file://{p}")
        return True
    except Exception:
        return False
