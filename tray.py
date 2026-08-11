"""
tray.py – System tray icon (StatusNotifierItem) for smolplayer.

Provides a system tray icon with a right-click context menu containing "Quit smolplayer".
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Callable, Optional
from config import open_config_file
from constants import (
    APP_NAME,
    APP_ID,
    SNI_IFACE,
    MENU_IFACE,
    WATCHER_BUS,
    WATCHER_PATH,
    SNI_OBJ_PATH,
    MENU_OBJ_PATH,
)

if TYPE_CHECKING:
    from player import PlayerEngine, TrackInfo
    from playlist import PlaylistManager

try:
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    DBusGMainLoop(set_as_default=True)
    _DBUS_OK = True
except Exception:
    _DBUS_OK = False

_SNI_IFACE   = SNI_IFACE
_MENU_IFACE  = MENU_IFACE
_PROPS_IFACE = "org.freedesktop.DBus.Properties"
_WATCHER_BUS = WATCHER_BUS
_WATCHER_PATH = WATCHER_PATH


class TrayService:
    def __init__(
        self,
        engine: PlayerEngine,
        playlist: PlaylistManager,
        on_quit: Optional[Callable[[], None]] = None,
        on_open_config: Optional[Callable[[], None]] = None,
    ) -> None:
        self._engine = engine
        self._playlist = playlist
        self._on_quit = on_quit
        self._on_open_config = on_open_config or open_config_file
        self._active = False

        if not _DBUS_OK:
            return

        try:
            self._bus = dbus.SessionBus()
            self._service_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
            self._bus_name = dbus.service.BusName(self._service_name, self._bus)

            self._sni_object = _SNIObject(self._bus_name, SNI_OBJ_PATH, self)
            self._menu_object = _MenuObject(self._bus_name, MENU_OBJ_PATH, self)

            # Register with StatusNotifierWatcher if present on desktop session bus
            try:
                watcher_obj = self._bus.get_object(_WATCHER_BUS, _WATCHER_PATH)
                watcher = dbus.Interface(watcher_obj, _WATCHER_BUS)
                watcher.RegisterStatusNotifierItem(self._service_name)
            except Exception:
                pass

            self._active = True
        except Exception:
            self._active = False

    def notify_track(self, info: Optional[TrackInfo]) -> None:
        if not self._active:
            return
        try:
            self._sni_object.NewToolTip()
            self._sni_object.NewTitle()
            self._menu_object.LayoutUpdated(1, 0)
        except Exception:
            pass

    def notify_state(self, state: str) -> None:
        if not self._active:
            return
        try:
            self._sni_object.NewStatus()
            self._sni_object.NewToolTip()
            self._menu_object.LayoutUpdated(1, 0)
        except Exception:
            pass

    def get_tooltip_text(self) -> str:
        track = self._engine.track
        if not track:
            return f"{APP_NAME} - Stopped"
        state_str = "Playing" if self._engine.state == "playing" else "Paused"
        title = track.title or os.path.basename(track.path)
        artist = f" - {track.artist}" if track.artist else ""
        return f"{title}{artist} ({state_str})"


if _DBUS_OK:

    class _SNIObject(dbus.service.Object):
        def __init__(self, bus_name: dbus.service.BusName, path: str, service: TrayService) -> None:
            super().__init__(bus_name, path)
            self._service = service

        @dbus.service.method(_PROPS_IFACE, in_signature="ss", out_signature="v")
        def Get(self, interface_name: str, property_name: str) -> Any:
            return self.GetAll(interface_name).get(property_name, "")

        @dbus.service.method(_PROPS_IFACE, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface_name: str) -> dict:
            if interface_name == _SNI_IFACE:
                tooltip_msg = self._service.get_tooltip_text()
                return {
                    "Category": dbus.String("ApplicationStatus"),
                    "Id": dbus.String(APP_NAME),
                    "Title": dbus.String(APP_NAME),
                    "Status": dbus.String("Active"),
                    "IconName": dbus.String(APP_ID),
                    "OverlayIconName": dbus.String(""),
                    "AttentionIconName": dbus.String(""),
                    "ToolTip": dbus.Struct(
                        (APP_ID, dbus.Array([], signature="(iiay)"), APP_NAME, tooltip_msg),
                        signature="sa(iiay)ss",
                    ),
                    "ItemIsMenu": dbus.Boolean(True),
                    "Menu": dbus.ObjectPath(MENU_OBJ_PATH),
                    "ContextMenu": dbus.ObjectPath(MENU_OBJ_PATH),
                }
            return {}

        @dbus.service.method(_SNI_IFACE, in_signature="ii", out_signature="")
        def Activate(self, x: int, y: int) -> None:
            pass

        @dbus.service.method(_SNI_IFACE, in_signature="ii", out_signature="")
        def SecondaryActivate(self, x: int, y: int) -> None:
            pass

        @dbus.service.method(_SNI_IFACE, in_signature="ii", out_signature="")
        def ContextMenu(self, x: int, y: int) -> None:
            try:
                self._service._menu_object.LayoutUpdated(1, 0)
            except Exception:
                pass

        @dbus.service.method(_SNI_IFACE, in_signature="is", out_signature="")
        def Scroll(self, delta: int, orientation: str) -> None:
            pass

        @dbus.service.signal(_SNI_IFACE, signature="")
        def NewTitle(self) -> None: pass

        @dbus.service.signal(_SNI_IFACE, signature="")
        def NewIcon(self) -> None: pass

        @dbus.service.signal(_SNI_IFACE, signature="")
        def NewStatus(self) -> None: pass

        @dbus.service.signal(_SNI_IFACE, signature="")
        def NewToolTip(self) -> None: pass


    class _MenuObject(dbus.service.Object):
        def __init__(self, bus_name: dbus.service.BusName, path: str, service: TrayService) -> None:
            super().__init__(bus_name, path)
            self._service = service

        @dbus.service.method(_MENU_IFACE, in_signature="iias", out_signature="u(ia{sv}av)")
        def GetLayout(self, parent_id: int, recursion_depth: int, property_names: list[str]) -> tuple:
            def make_item(id_num: int, props: dict, children: Optional[list] = None) -> tuple:
                if children is None:
                    c_arr = dbus.Array([], signature="v")
                else:
                    c_arr = dbus.Array(children, signature="v")
                d_props = dbus.Dictionary(props, signature="sv")
                return (dbus.Int32(id_num), d_props, c_arr)

            config_item = make_item(1, {"label": "Open Config File", "icon-name": "preferences-system"})
            quit_item = make_item(2, {"label": f"Quit {APP_NAME}", "icon-name": "application-exit"})
            root = make_item(0, {}, [config_item, quit_item])
            return (dbus.UInt32(1), root)

        @dbus.service.method(_MENU_IFACE, in_signature="i", out_signature="b")
        def AboutToShow(self, id: int) -> bool:
            return False

        @dbus.service.method(_MENU_IFACE, in_signature="aias", out_signature="a(ia{sv})")
        def GetGroupProperties(self, ids: list[int], property_names: list[str]) -> list:
            return []

        @dbus.service.method(_MENU_IFACE, in_signature="isvu", out_signature="")
        def Event(self, id: int, event_id: str, data: Any, timestamp: int) -> None:
            if event_id == "clicked":
                if id == 1:
                    if self._service._on_open_config:
                        self._service._on_open_config()
                elif id == 2:
                    if self._service._on_quit:
                        self._service._on_quit()


        @dbus.service.signal(_MENU_IFACE, signature="ui")
        def LayoutUpdated(self, revision: int, parent_id: int) -> None: pass
