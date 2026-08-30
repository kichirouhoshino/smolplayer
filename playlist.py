"""
playlist.py – Folder and playlist management for smolplayer.

Supports folder scanning (single and recursive), playlist files (.m3u, .m3u8, .pls, .xspf),
and CUE sheet parsing (.cue) with sub-track playback.
"""

from __future__ import annotations

import os
import random
import threading
from typing import Callable, List, Optional, Union

from utils import (
    AUDIO_EXTS,
    PLAYLIST_EXTS,
    CUE_EXTS,
    is_audio_file,
    is_playlist_file,
    is_cue_file,
    natural_sort_key,
    normalize_file_path,
)
from config import get_config, load_toggles_state, save_toggles_state, normalize_loop_status
from probing import TrackInfo, _probe_metadata

# Re-exports from parsers for modularity and backwards compatibility
from parsers import (
    parse_cue_time,
    _unquote_cue_str,
    _resolve_cue_audio_file,
    parse_cue,
    parse_m3u,
    parse_pls,
    parse_xspf,
    parse_playlist_file,
    _item_path,
    sort_audio_files,
    _is_valid_audio_file,
    scan_audio_files_single_folder,
    scan_audio_files_recursive,
)


def _generate_smart_shuffle_indices(items: List[Union[str, TrackInfo]], current_idx: int) -> List[int]:
    """
    Industry-established Smart Shuffle algorithm:
    Interleaves tracks from different artists/albums to prevent playing the same artist back-to-back.
    """
    total = len(items)
    if total <= 2:
        indices = list(range(total))
        if 0 <= current_idx < total:
            indices.remove(current_idx)
            return [current_idx] + indices
        random.shuffle(indices)
        return indices

    groups: dict[str, list[int]] = {}
    artists_map: dict[int, str] = {}
    for i in range(total):
        if i == current_idx:
            continue
        item = items[i]
        if isinstance(item, TrackInfo):
            artist = item.artist or item.album or "unknown"
        else:
            meta = _probe_metadata(item) or {}
            artist = meta.get("artist") or meta.get("album") or "unknown"
        artists_map[i] = artist
        groups.setdefault(artist, []).append(i)

    for k in groups:
        random.shuffle(groups[k])

    shuffled_groups = list(groups.values())
    random.shuffle(shuffled_groups)

    result = [current_idx] if (0 <= current_idx < total) else []
    last_artist = None
    if 0 <= current_idx < total:
        curr_item = items[current_idx]
        if isinstance(curr_item, TrackInfo):
            last_artist = curr_item.artist or curr_item.album
        else:
            m = _probe_metadata(curr_item) or {}
            last_artist = m.get("artist") or m.get("album")

    while any(shuffled_groups):
        eligible = [
            g for g in shuffled_groups
            if g and (artists_map.get(g[0]) != last_artist or len(shuffled_groups) == 1)
        ]
        if not eligible:
            eligible = [g for g in shuffled_groups if g]
        chosen_group = random.choice(eligible)
        item_idx = chosen_group.pop(0)
        result.append(item_idx)
        last_artist = artists_map.get(item_idx)
        shuffled_groups = [g for g in shuffled_groups if g]

    return result


class PlaylistManager:
    """
    Manages an automatic playlist based on files, folders, or playlist/CUE sheets.
    """

    def __init__(self) -> None:
        self._items: List[Union[str, TrackInfo]] = []
        self._current_index: int = -1
        self._shuffle: bool = False
        self._loop_status: str = "None"   # "None" | "Track" | "Playlist"
        self._shuffled_indices: List[int] = []
        self._shuffled_pos: int = 0
        self.on_toggles_changed: Optional[Callable[[bool, str], None]] = None

        cfg = get_config()
        if cfg.remember_toggles:
            self._shuffle, self._loop_status = load_toggles_state()

    @property
    def items(self) -> List[Union[str, TrackInfo]]:
        return list(self._items)

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    @shuffle.setter
    def shuffle(self, enabled: bool) -> None:
        if self._shuffle == enabled:
            return
        self._shuffle = enabled
        if enabled:
            self._generate_shuffled_queue()
        else:
            self._shuffled_indices.clear()
            self._shuffled_pos = 0

        cfg = get_config()
        if cfg.remember_toggles:
            save_toggles_state(self._shuffle, self._loop_status)
        if self.on_toggles_changed:
            self.on_toggles_changed(self._shuffle, self._loop_status)

    @property
    def loop_status(self) -> str:
        return self._loop_status

    @loop_status.setter
    def loop_status(self, status: str) -> None:
        norm = normalize_loop_status(status)
        if self._loop_status == norm:
            return
        self._loop_status = norm
        cfg = get_config()
        if cfg.remember_toggles:
            save_toggles_state(self._shuffle, self._loop_status)
        if self.on_toggles_changed:
            self.on_toggles_changed(self._shuffle, self._loop_status)

    def __len__(self) -> int:
        return len(self._items)

    def _generate_shuffled_queue(self) -> None:
        """
        Puts current track at position 0, followed by shuffle queue based on config.shuffle_algo.
        """
        if not self._items:
            self._shuffled_indices = []
            self._shuffled_pos = 0
            return

        total = len(self._items)
        curr = self._current_index if (0 <= self._current_index < total) else 0

        cfg = get_config()
        algo = cfg.shuffle_algo

        if algo == 1:
            # Smart Shuffle (Artist-spaced)
            self._shuffled_indices = _generate_smart_shuffle_indices(self._items, curr)
            self._shuffled_pos = 0
        elif algo == 2:
            # True Random (Picked dynamically per track in get_next)
            indices = list(range(total))
            if 0 <= curr < total:
                indices.remove(curr)
                self._shuffled_indices = [curr] + indices
            else:
                self._shuffled_indices = indices
            self._shuffled_pos = 0
        else:
            # Fisher-Yates (Modern Knuth Shuffle - Industry Standard, default)
            others = [i for i in range(total) if i != curr]
            random.shuffle(others)
            self._shuffled_indices = [curr] + others
            self._shuffled_pos = 0

    def load_file_or_folder(self, path: str) -> Optional[Union[str, TrackInfo]]:
        """
        Build playlist from path.
        - If path is a directory: scans based on recurse_folderopen setting.
        - If path is a playlist or CUE file: loads and plays playlist entries.
        - If path is a single audio file: plays immediately and scans folder in background.
        """
        abs_path = normalize_file_path(path)
        cfg = get_config()

        # Handle toggles on each new play session
        old_shuffle = self._shuffle
        old_loop = self._loop_status
        if cfg.remember_toggles:
            self._shuffle, self._loop_status = load_toggles_state()
        else:
            self._shuffle = False
            self._loop_status = "None"

        if is_cue_file(abs_path) and cfg.cue_noshuffle:
            self._shuffle = False

        if (old_shuffle != self._shuffle or old_loop != self._loop_status) and self.on_toggles_changed:
            self.on_toggles_changed(self._shuffle, self._loop_status)

        if os.path.isdir(abs_path):
            rec = bool(cfg.recurse_folderopen)
            self._scan_folder_sync(abs_path, target_file=None, recursive=rec)
            return self.current_track()
        elif is_playlist_file(abs_path):
            playlist_tracks = parse_playlist_file(abs_path)
            if cfg.cue_order and playlist_tracks:
                playlist_tracks = sort_audio_files(playlist_tracks, cfg.sort_method)
            self._items = playlist_tracks
            if self._items:
                if self._shuffle:
                    self._current_index = random.randint(0, len(self._items) - 1)
                    self._generate_shuffled_queue()
                else:
                    self._current_index = 0
                    self._shuffled_indices.clear()
                    self._shuffled_pos = 0
            else:
                self._current_index = -1
            return self.current_track()
        else:
            folder = os.path.dirname(abs_path)
            target_file = abs_path
            self._items = [target_file]
            self._current_index = 0
            if self._shuffle:
                self._generate_shuffled_queue()
            else:
                self._shuffled_indices.clear()
                self._shuffled_pos = 0

            rec = bool(cfg.recurse_fileopen)
            threading.Thread(
                target=self._scan_folder_async,
                args=(folder, target_file, rec),
                daemon=True,
                name="smolplayer-playlist-scan",
            ).start()
            return target_file

    def _scan_folder_sync(self, folder: str, target_file: Optional[str], recursive: bool = False) -> None:
        """Synchronous folder scan (recursive for dirs, non-recursive for single files)."""
        cfg = get_config()
        sort_m = cfg.sort_method
        audio_files = scan_audio_files_recursive(folder, sort_method=sort_m) if recursive else scan_audio_files_single_folder(folder, sort_method=sort_m)

        if not audio_files and target_file and os.path.isfile(target_file):
            audio_files = [target_file]

        self._items = audio_files

        if target_file:
            match_idx = -1
            for i, it in enumerate(self._items):
                if _item_path(it) == target_file:
                    match_idx = i
                    break
            if match_idx >= 0:
                self._current_index = match_idx
            elif self._items:
                self._current_index = random.randint(0, len(self._items) - 1) if self._shuffle else 0
            else:
                self._current_index = -1
        elif self._items:
            if self._shuffle:
                self._current_index = random.randint(0, len(self._items) - 1)
            else:
                self._current_index = 0
        else:
            self._current_index = -1

        if self._shuffle:
            self._generate_shuffled_queue()

    def _scan_folder_async(self, folder: str, target_file: Optional[str], recursive: bool = False) -> None:
        """Asynchronous folder scan to populate playlist without holding up audio startup."""
        try:
            cfg = get_config()
            sort_m = cfg.sort_method
            audio_files = scan_audio_files_recursive(folder, sort_method=sort_m) if recursive else scan_audio_files_single_folder(folder, sort_method=sort_m)

            if not audio_files and target_file and os.path.isfile(target_file):
                audio_files = [target_file]

            if audio_files:
                curr_track = self.current_track()
                curr_path = _item_path(curr_track) if curr_track else None
                self._items = audio_files

                match_idx = -1
                if curr_path:
                    for i, it in enumerate(self._items):
                        if _item_path(it) == curr_path:
                            match_idx = i
                            break
                if match_idx < 0 and target_file:
                    for i, it in enumerate(self._items):
                        if _item_path(it) == target_file:
                            match_idx = i
                            break

                if match_idx >= 0:
                    self._current_index = match_idx
                else:
                    self._current_index = 0

                if self._shuffle:
                    self._generate_shuffled_queue()
        except Exception:
            pass

    def current_track(self) -> Optional[Union[str, TrackInfo]]:
        if 0 <= self._current_index < len(self._items):
            return self._items[self._current_index]
        return None

    def set_index(self, index: int) -> Optional[Union[str, TrackInfo]]:
        if 0 <= index < len(self._items):
            self._current_index = index
            if self._shuffle:
                self._generate_shuffled_queue()
            return self._items[index]
        return None

    def peek_next(self, auto_advance: bool = False) -> Optional[Union[str, TrackInfo]]:
        """Peek at the upcoming track without modifying the current playlist position."""
        if not self._items:
            return None

        if auto_advance and self._loop_status == "Track":
            return self.current_track()

        cfg = get_config()
        if self._shuffle:
            if cfg.shuffle_algo == 2:
                return self._items[0] if self._items else None
            elif self._shuffled_indices:
                if self._shuffled_pos + 1 < len(self._shuffled_indices):
                    return self._items[self._shuffled_indices[self._shuffled_pos + 1]]
                elif self._loop_status == "Playlist":
                    return self._items[self._shuffled_indices[0]] if self._shuffled_indices else None
                else:
                    return None
        else:
            if self._current_index + 1 < len(self._items):
                return self._items[self._current_index + 1]
            elif self._loop_status == "Playlist":
                return self._items[0]
            else:
                return None

    def get_next(self, auto_advance: bool = False) -> Optional[Union[str, TrackInfo]]:
        if not self._items:
            return None

        if auto_advance and self._loop_status == "Track":
            return self.current_track()

        cfg = get_config()
        if self._shuffle:
            if cfg.shuffle_algo == 2:
                # True Random
                self._current_index = random.randint(0, len(self._items) - 1)
                return self._items[self._current_index]
            elif self._shuffled_indices:
                if self._shuffled_pos + 1 < len(self._shuffled_indices):
                    self._shuffled_pos += 1
                elif self._loop_status == "Playlist":
                    self._generate_shuffled_queue()
                else:
                    return None
                self._current_index = self._shuffled_indices[self._shuffled_pos]
                return self._items[self._current_index]
        else:
            if self._current_index + 1 < len(self._items):
                self._current_index += 1
            elif self._loop_status == "Playlist":
                self._current_index = 0
            else:
                return None
            return self._items[self._current_index]

    def get_previous(self) -> Optional[Union[str, TrackInfo]]:
        if not self._items:
            return None

        if self._shuffle and self._shuffled_indices:
            if self._shuffled_pos - 1 >= 0:
                self._shuffled_pos -= 1
            elif self._loop_status == "Playlist":
                self._shuffled_pos = len(self._shuffled_indices) - 1
            else:
                return None
            self._current_index = self._shuffled_indices[self._shuffled_pos]
            return self._items[self._current_index]

        else:
            if self._current_index - 1 >= 0:
                self._current_index -= 1
            elif self._loop_status == "Playlist":
                self._current_index = len(self._items) - 1
            else:
                return None
            return self._items[self._current_index]

    def can_go_next(self) -> bool:
        if not self._items:
            return False
        if self._loop_status in ("Playlist", "Track"):
            return True
        if len(self._items) <= 1:
            return False
        if self._shuffle and self._shuffled_indices:
            return self._shuffled_pos + 1 < len(self._shuffled_indices)
        return self._current_index + 1 < len(self._items)

    def can_go_previous(self) -> bool:
        if not self._items:
            return False
        if self._loop_status in ("Playlist", "Track"):
            return True
        if len(self._items) <= 1:
            return False
        if self._shuffle and self._shuffled_indices:
            return self._shuffled_pos > 0
        return self._current_index > 0
