"""
playlist.py – Folder-based playlist management for smolplayer.
"""

from __future__ import annotations

import os
import random
import threading
from typing import List, Optional
from utils import AUDIO_EXTS, natural_sort_key, normalize_file_path


from config import get_config


def sort_audio_files(audio_files: List[str], sort_method: int, folder: Optional[str] = None) -> List[str]:
    """Sort audio files by filename, title, disc & track number, or artist & title."""
    if sort_method == 0:
        if folder:
            def rel_sort_key(p: str) -> list:
                rel = os.path.relpath(p, folder)
                return [natural_sort_key(part) for part in rel.split(os.sep)]
            audio_files.sort(key=rel_sort_key)
        else:
            audio_files.sort(key=lambda p: natural_sort_key(os.path.basename(p)))
    else:
        from player import _probe_metadata
        metadata_map = {}
        for p in audio_files:
            meta = _probe_metadata(p) or {}
            metadata_map[p] = meta

        def meta_sort_key(p: str) -> tuple:
            m = metadata_map.get(p, {})
            title = m.get("title") or os.path.basename(p)
            artist = m.get("artist") or ""
            disc = m.get("disc") or 1
            track = m.get("track") or 0
            fname = os.path.basename(p)

            if sort_method == 1:
                return (title.lower(), fname.lower())
            elif sort_method == 2:
                return (disc, track, fname.lower())
            elif sort_method == 3:
                return (artist.lower(), title.lower(), fname.lower())
            return (fname.lower(),)

        audio_files.sort(key=meta_sort_key)

    return audio_files


def _is_valid_audio_file(filename: str) -> bool:
    """Check if filename is not hidden and has a supported audio extension."""
    if filename.startswith('.'):
        return False
    return os.path.splitext(filename)[1].lower() in AUDIO_EXTS


def scan_audio_files_single_folder(folder: str, sort_method: int = 0) -> List[str]:
    """
    Non-recursive scan of a single folder for supported audio files.
    Used when double-clicking an individual audio file.
    """
    audio_files: List[str] = []
    try:
        for entry in os.scandir(folder):
            if entry.is_file() and _is_valid_audio_file(entry.name):
                audio_files.append(entry.path)
    except OSError:
        pass

    return sort_audio_files(audio_files, sort_method, folder=None)


def scan_audio_files_recursive(folder: str, sort_method: int = 0) -> List[str]:
    """
    Recursively scans folder and all subdirectories for supported audio files.
    Used when opening a folder directly (e.g. 'Open folder with smolplayer').
    """
    audio_files: List[str] = []
    try:
        for root_dir, dirs, files in os.walk(folder, followlinks=True):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for f in files:
                if _is_valid_audio_file(f):
                    audio_files.append(os.path.join(root_dir, f))
    except OSError:
        pass

    return sort_audio_files(audio_files, sort_method, folder=folder)


def _generate_smart_shuffle_indices(items: List[str], current_idx: int) -> List[int]:
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

    from player import _probe_metadata

    groups: dict[str, list[int]] = {}
    artists_map: dict[int, str] = {}
    for i in range(total):
        if i == current_idx:
            continue
        meta = _probe_metadata(items[i]) or {}
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
        m = _probe_metadata(items[current_idx]) or {}
        last_artist = m.get("artist") or m.get("album")

    while any(shuffled_groups):
        eligible = [
            g for g in shuffled_groups
            if g and (artists_map.get(g[0]) != last_artist or len(shuffled_groups) == 1)
        ]
        if not eligible:
            eligible = [g for g in shuffled_groups if g]
        chosen_group = random.choice(eligible)
        item = chosen_group.pop(0)
        result.append(item)
        last_artist = artists_map.get(item)
        shuffled_groups = [g for g in shuffled_groups if g]

    return result


class PlaylistManager:
    """
    Manages an automatic playlist based on the folder of the currently playing track.
    """

    def __init__(self) -> None:
        self._items: List[str] = []
        self._current_index: int = -1
        self._shuffle: bool = False
        self._loop_status: str = "None"   # "None" | "Track" | "Playlist"
        self._shuffled_indices: List[int] = []
        self._shuffled_pos: int = 0

        cfg = get_config()
        if cfg.remember_toggles:
            self._shuffle, self._loop_status = load_toggles_state()

    @property
    def items(self) -> List[str]:
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

    @property
    def loop_status(self) -> str:
        return self._loop_status

    @loop_status.setter
    def loop_status(self, status: str) -> None:
        if status in ("None", "Track", "Playlist"):
            self._loop_status = status
            cfg = get_config()
            if cfg.remember_toggles:
                save_toggles_state(self._shuffle, self._loop_status)

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

    def load_file_or_folder(self, path: str) -> Optional[str]:
        """
        Build playlist from path.
        If path is a directory, scans based on recurse_folderopen setting.
        If path is a file, scans based on recurse_fileopen setting.
        """
        abs_path = normalize_file_path(path)
        cfg = get_config()

        if os.path.isdir(abs_path):
            rec = bool(cfg.recurse_folderopen)
            self._scan_folder_sync(abs_path, target_file=None, recursive=rec)
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

        if target_file and target_file in self._items:
            self._current_index = self._items.index(target_file)
        elif self._items:
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
                self._items = audio_files
                if curr_track and curr_track in self._items:
                    self._current_index = self._items.index(curr_track)
                elif target_file and target_file in self._items:
                    self._current_index = self._items.index(target_file)
                else:
                    self._current_index = 0

                if self._shuffle:
                    self._generate_shuffled_queue()
        except Exception:
            pass

    def current_track(self) -> Optional[str]:
        if 0 <= self._current_index < len(self._items):
            return self._items[self._current_index]
        return None

    def set_index(self, index: int) -> Optional[str]:
        if 0 <= index < len(self._items):
            self._current_index = index
            if self._shuffle:
                self._generate_shuffled_queue()
            return self._items[index]
        return None

    def get_next(self, auto_advance: bool = False) -> Optional[str]:
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
            return self._items[self._current_index]

    def get_previous(self) -> Optional[str]:
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
        if len(self._items) <= 1:
            return False
        if self._loop_status in ("Playlist", "Track"):
            return True
        if self._shuffle and self._shuffled_indices:
            return self._shuffled_pos + 1 < len(self._shuffled_indices)
        return self._current_index + 1 < len(self._items)

    def can_go_previous(self) -> bool:
        if len(self._items) <= 1:
            return False
        if self._loop_status in ("Playlist", "Track"):
            return True
        if self._shuffle and self._shuffled_indices:
            return self._shuffled_pos > 0
        return self._current_index > 0
