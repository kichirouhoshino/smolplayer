"""
playlist.py – Folder and playlist management for smolplayer.

Supports folder scanning (single and recursive), playlist files (.m3u, .m3u8, .pls, .xspf),
and CUE sheet parsing (.cue) with sub-track playback.
"""

from __future__ import annotations

import os
import random
import re
import threading
import xml.etree.ElementTree as ET
from typing import Any, Callable, List, Optional, Set, Tuple, Union
from urllib.parse import unquote, urlparse

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
from player import TrackInfo


def parse_cue_time(time_str: str) -> float:
    """
    Parse a CUE timestamp (MM:SS:FF or MM:SS) into seconds.
    FF is frames (75 frames per second on Audio CD standard).
    """
    parts = time_str.strip().split(":")
    try:
        if len(parts) == 3:
            mm, ss, ff = int(parts[0]), int(parts[1]), int(parts[2])
            return mm * 60.0 + ss + (ff / 75.0)
        elif len(parts) == 2:
            mm, ss = int(parts[0]), int(parts[1])
            return mm * 60.0 + ss
    except ValueError:
        pass
    return 0.0


def _unquote_cue_str(val: str) -> str:
    """Strip surrounding quotes and whitespace from a CUE string."""
    val = val.strip()
    if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
        return val[1:-1]
    return val


def _resolve_cue_audio_file(cue_dir: str, filename: str, cue_filename: str = "") -> str:
    """
    Resolve the backing audio file referenced in a CUE sheet.
    Handles relative paths, case sensitivity, extension differences (e.g. .wav vs .flac),
    and single-file fallbacks.
    """
    cleaned_name = _unquote_cue_str(filename).replace("\\", "/")
    cand = os.path.normpath(os.path.join(cue_dir, cleaned_name))
    if os.path.isfile(cand):
        return cand

    # Case-insensitive search for exact filename in cue_dir
    base_lower = os.path.basename(cleaned_name).lower()
    try:
        with os.scandir(cue_dir) as entries:
            for entry in entries:
                if entry.is_file() and entry.name.lower() == base_lower:
                    return entry.path
    except OSError:
        pass

    # Check for same stem with any supported audio extension
    stem = os.path.splitext(os.path.basename(cleaned_name))[0].lower()
    try:
        with os.scandir(cue_dir) as entries:
            for entry in entries:
                if entry.is_file():
                    entry_stem, entry_ext = os.path.splitext(entry.name)
                    if entry_stem.lower() == stem and entry_ext.lower() in AUDIO_EXTS:
                        return entry.path
    except OSError:
        pass

    # Check if an audio file with same stem as the cue file itself exists
    if cue_filename:
        cue_stem = os.path.splitext(os.path.basename(cue_filename))[0].lower()
        try:
            with os.scandir(cue_dir) as entries:
                for entry in entries:
                    if entry.is_file():
                        entry_stem, entry_ext = os.path.splitext(entry.name)
                        if entry_stem.lower() == cue_stem and entry_ext.lower() in AUDIO_EXTS:
                            return entry.path
        except OSError:
            pass

    # If there is only one audio file in cue_dir, use it
    try:
        with os.scandir(cue_dir) as entries:
            audio_entries = [
                e.path for e in entries
                if e.is_file() and is_audio_file(e.name)
            ]
            if len(audio_entries) == 1:
                return audio_entries[0]
    except OSError:
        pass

    return cand


def parse_cue(cue_path: str) -> List[TrackInfo]:
    """
    Parse a CUE sheet (.cue) into a list of TrackInfo objects with sub-track bounds.
    """
    abs_cue = normalize_file_path(cue_path)
    if not os.path.isfile(abs_cue):
        return []

    cue_dir = os.path.dirname(abs_cue)
    cue_filename = os.path.basename(abs_cue)

    # Read CUE file with UTF-8 (fallback Latin-1)
    content = ""
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            with open(abs_cue, "r", encoding=enc) as f:
                content = f.read()
            break
        except (UnicodeDecodeError, OSError):
            continue

    if not content:
        return []

    content = content.lstrip("\ufeff")

    album_artist = ""
    album_title = ""
    album_gain: Optional[float] = None
    album_peak: Optional[float] = None
    current_file = ""
    current_file_path = ""

    raw_tracks: List[dict] = []
    current_track: Optional[dict] = None

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        # ReplayGain album tags
        m_rg_alb = re.search(r"^REM\s+REPLAYGAIN_ALBUM_GAIN\s+([-+]?\d+(?:\.\d+)?)", line, re.IGNORECASE)
        if m_rg_alb:
            try:
                album_gain = float(m_rg_alb.group(1))
            except ValueError:
                pass
            continue

        m_rg_albp = re.search(r"^REM\s+REPLAYGAIN_ALBUM_PEAK\s+([\d\.]+)", line, re.IGNORECASE)
        if m_rg_albp:
            try:
                album_peak = float(m_rg_albp.group(1))
            except ValueError:
                pass
            continue

        # ReplayGain track tags
        if current_track is not None:
            m_rg_trk = re.search(r"^REM\s+REPLAYGAIN_TRACK_GAIN\s+([-+]?\d+(?:\.\d+)?)", line, re.IGNORECASE)
            if m_rg_trk:
                try:
                    current_track["track_gain"] = float(m_rg_trk.group(1))
                except ValueError:
                    pass
                continue

            m_rg_trkp = re.search(r"^REM\s+REPLAYGAIN_TRACK_PEAK\s+([\d\.]+)", line, re.IGNORECASE)
            if m_rg_trkp:
                try:
                    current_track["track_peak"] = float(m_rg_trkp.group(1))
                except ValueError:
                    pass
                continue

        # FILE command
        m_file = re.match(r'^FILE\s+"([^"]+)"\s*(.*)$', line, re.IGNORECASE) or re.match(r"^FILE\s+(\S+)\s*(.*)$", line, re.IGNORECASE)
        if m_file:
            current_file = m_file.group(1)
            current_file_path = _resolve_cue_audio_file(cue_dir, current_file, cue_filename)
            continue

        # TRACK command
        m_track = re.match(r"^TRACK\s+(\d+)\s+(.*)$", line, re.IGNORECASE)
        if m_track:
            track_num = int(m_track.group(1))
            current_track = {
                "num": track_num,
                "file_path": current_file_path or _resolve_cue_audio_file(cue_dir, "", cue_filename),
                "title": "",
                "artist": "",
                "index00": None,
                "index01": None,
                "pregap": None,
                "track_gain": None,
                "track_peak": None,
            }
            raw_tracks.append(current_track)
            continue

        # TITLE command
        m_title = re.match(r'^TITLE\s+"([^"]+)"', line, re.IGNORECASE) or re.match(r"^TITLE\s+(.*)$", line, re.IGNORECASE)
        if m_title:
            val = _unquote_cue_str(m_title.group(1))
            if current_track is None:
                album_title = val
            else:
                current_track["title"] = val
            continue

        # PERFORMER command
        m_perf = re.match(r'^PERFORMER\s+"([^"]+)"', line, re.IGNORECASE) or re.match(r"^PERFORMER\s+(.*)$", line, re.IGNORECASE)
        if m_perf:
            val = _unquote_cue_str(m_perf.group(1))
            if current_track is None:
                album_artist = val
            else:
                current_track["artist"] = val
            continue

        # INDEX command
        if current_track is not None:
            m_idx = re.match(r"^INDEX\s+(\d+)\s+([\d:]+)", line, re.IGNORECASE)
            if m_idx:
                idx_num = int(m_idx.group(1))
                idx_time = parse_cue_time(m_idx.group(2))
                if idx_num == 0:
                    current_track["index00"] = idx_time
                elif idx_num == 1:
                    current_track["index01"] = idx_time
                continue

            m_pregap = re.match(r"^PREGAP\s+([\d:]+)", line, re.IGNORECASE)
            if m_pregap:
                current_track["pregap"] = parse_cue_time(m_pregap.group(1))
                continue

    if not raw_tracks:
        return []

    for t in raw_tracks:
        if t["index00"] is None and t.get("pregap") is not None:
            idx01 = t["index01"] if t["index01"] is not None else 0.0
            t["index00"] = max(0.0, idx01 - t["pregap"])

    # Calculate start_time, end_time, and duration for each track
    results: List[TrackInfo] = []
    # Group tracks by file_path
    groups: dict[str, List[dict]] = {}
    for t in raw_tracks:
        groups.setdefault(t["file_path"], []).append(t)

    for file_p, trk_list in groups.items():
        for i, t in enumerate(trk_list):
            start = t["index01"] if t["index01"] is not None else (t["index00"] if t["index00"] is not None else 0.0)
            end = None
            if i + 1 < len(trk_list):
                nxt = trk_list[i + 1]
                end = nxt["index00"] if nxt["index00"] is not None else (nxt["index01"] if nxt["index01"] is not None else None)

            dur = max(0.0, end - start) if end is not None else 0.0
            trk_title = t["title"] or f"Track {t['num']}"
            trk_artist = t["artist"] or album_artist

            info = TrackInfo(
                path=file_p,
                title=trk_title,
                artist=trk_artist,
                album=album_title,
                duration=dur,
                track_gain_db=t["track_gain"],
                album_gain_db=album_gain,
                track_peak=t["track_peak"],
                album_peak=album_peak,
                start_time=start,
                end_time=end,
                track_number=t["num"],
                cue_path=abs_cue,
            )
            results.append(info)

    return results


def parse_m3u(m3u_path: str, visited: Optional[Set[str]] = None) -> List[Union[str, TrackInfo]]:
    """
    Parse an M3U or M3U8 playlist into a list of file paths or TrackInfo objects.
    Supports #EXTINF, #EXTALB, #EXTART, relative paths, and nested CUE sheets.
    """
    visited = visited if visited is not None else set()
    abs_m3u = normalize_file_path(m3u_path)
    if abs_m3u in visited or not os.path.isfile(abs_m3u):
        return []
    visited.add(abs_m3u)

    m3u_dir = os.path.dirname(abs_m3u)

    content = ""
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            with open(abs_m3u, "r", encoding=enc) as f:
                content = f.read()
            break
        except (UnicodeDecodeError, OSError):
            continue

    if not content:
        return []

    items: List[Union[str, TrackInfo]] = []
    pending_title = ""
    pending_artist = ""
    pending_album = ""
    pending_dur = 0.0

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith("#EXTINF:"):
            # Format: #EXTINF:seconds,Artist - Title or #EXTINF:seconds,Title
            rest = line[8:].strip()
            if "," in rest:
                dur_str, name_str = rest.split(",", 1)
                try:
                    pending_dur = max(0.0, float(dur_str))
                except ValueError:
                    pending_dur = 0.0
                name_str = name_str.strip()
                if " - " in name_str:
                    pending_artist, pending_title = name_str.split(" - ", 1)
                else:
                    pending_title = name_str
            else:
                try:
                    pending_dur = max(0.0, float(rest))
                except ValueError:
                    pending_dur = 0.0
            continue

        if line.startswith("#EXTALB:"):
            pending_album = line[8:].strip()
            continue

        if line.startswith("#EXTART:"):
            pending_artist = line[8:].strip()
            continue

        if line.startswith("#"):
            continue

        # File entry line
        entry_path = line
        if entry_path.startswith("file://"):
            entry_path = unquote(urlparse(entry_path).path)
        elif not entry_path.startswith(("http://", "https://")):
            entry_path = unquote(entry_path).replace("\\", "/")

        if not entry_path.startswith(("http://", "https://")) and not os.path.isabs(entry_path):
            entry_path = os.path.normpath(os.path.join(m3u_dir, entry_path))

        if not entry_path.startswith(("http://", "https://")):
            entry_path = normalize_file_path(entry_path)

        if is_cue_file(entry_path) and os.path.isfile(entry_path):
            items.extend(parse_cue(entry_path))
        elif is_playlist_file(entry_path) and os.path.isfile(entry_path):
            items.extend(parse_playlist_file(entry_path, visited=visited))
        elif os.path.isfile(entry_path) or is_audio_file(entry_path) or entry_path.startswith(("http://", "https://")):
            if pending_title or pending_artist or pending_album or pending_dur > 0.0:
                items.append(TrackInfo(
                    path=entry_path,
                    title=pending_title,
                    artist=pending_artist,
                    album=pending_album,
                    duration=pending_dur,
                ))
            else:
                items.append(entry_path)

        pending_title = ""
        pending_artist = ""
        pending_album = ""
        pending_dur = 0.0

    return items


def parse_pls(pls_path: str, visited: Optional[Set[str]] = None) -> List[Union[str, TrackInfo]]:
    """
    Parse a PLS playlist file into a list of file paths or TrackInfo objects.
    """
    visited = visited if visited is not None else set()
    abs_pls = normalize_file_path(pls_path)
    if abs_pls in visited or not os.path.isfile(abs_pls):
        return []
    visited.add(abs_pls)

    pls_dir = os.path.dirname(abs_pls)

    content = ""
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            with open(abs_pls, "r", encoding=enc) as f:
                content = f.read()
            break
        except (UnicodeDecodeError, OSError):
            continue

    if not content:
        return []

    entries: dict[int, dict] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(file|title|length)(\d+)\s*=\s*(.*)$", line, re.IGNORECASE)
        if m:
            key_type = m.group(1).lower()
            idx = int(m.group(2))
            val = m.group(3).strip()
            entries.setdefault(idx, {})[key_type] = val

    items: List[Union[str, TrackInfo]] = []
    for idx in sorted(entries.keys()):
        data = entries[idx]
        file_val = data.get("file")
        if not file_val:
            continue

        if file_val.startswith("file://"):
            entry_path = unquote(urlparse(file_val).path)
        elif not file_val.startswith(("http://", "https://")):
            entry_path = unquote(file_val).replace("\\", "/")

        if not entry_path.startswith(("http://", "https://")) and not os.path.isabs(entry_path):
            entry_path = os.path.normpath(os.path.join(pls_dir, entry_path))

        if not entry_path.startswith(("http://", "https://")):
            entry_path = normalize_file_path(entry_path)

        title_val = data.get("title", "")
        length_val = data.get("length", "")
        dur = 0.0
        try:
            dur = max(0.0, float(length_val))
        except ValueError:
            pass

        artist_val = ""
        if title_val and " - " in title_val:
            artist_val, title_val = title_val.split(" - ", 1)

        if is_cue_file(entry_path) and os.path.isfile(entry_path):
            items.extend(parse_cue(entry_path))
        elif is_playlist_file(entry_path) and os.path.isfile(entry_path):
            items.extend(parse_playlist_file(entry_path, visited=visited))
        elif os.path.isfile(entry_path) or is_audio_file(entry_path) or entry_path.startswith(("http://", "https://")):
            if title_val or artist_val or dur > 0.0:
                items.append(TrackInfo(
                    path=entry_path,
                    title=title_val,
                    artist=artist_val,
                    duration=dur,
                ))
            else:
                items.append(entry_path)

    return items


def parse_xspf(xspf_path: str, visited: Optional[Set[str]] = None) -> List[Union[str, TrackInfo]]:
    """
    Parse an XML Shareable Playlist Format (.xspf) file.
    """
    visited = visited if visited is not None else set()
    abs_xspf = normalize_file_path(xspf_path)
    if abs_xspf in visited or not os.path.isfile(abs_xspf):
        return []
    visited.add(abs_xspf)

    xspf_dir = os.path.dirname(abs_xspf)
    items: List[Union[str, TrackInfo]] = []

    try:
        tree = ET.parse(abs_xspf)
        root = tree.getroot()
        # Find all track elements regardless of XML namespace
        for elem in root.iter():
            if elem.tag.endswith("track") and not elem.tag.endswith("trackList"):
                loc_elem = None
                title_elem = None
                creator_elem = None
                album_elem = None
                dur_elem = None

                for child in elem:
                    tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if tag == "location":
                        loc_elem = child
                    elif tag == "title":
                        title_elem = child
                    elif tag == "creator":
                        creator_elem = child
                    elif tag == "album":
                        album_elem = child
                    elif tag == "duration":
                        dur_elem = child

                if loc_elem is not None and loc_elem.text:
                    loc = loc_elem.text.strip()
                    if loc.startswith("file://"):
                        entry_path = unquote(urlparse(loc).path)
                    elif not loc.startswith(("http://", "https://")):
                        entry_path = unquote(loc).replace("\\", "/")
                    else:
                        entry_path = loc

                    if not entry_path.startswith(("http://", "https://")) and not os.path.isabs(entry_path):
                        entry_path = os.path.normpath(os.path.join(xspf_dir, entry_path))

                    if not entry_path.startswith(("http://", "https://")):
                        entry_path = normalize_file_path(entry_path)

                    title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
                    creator = creator_elem.text.strip() if creator_elem is not None and creator_elem.text else ""
                    album = album_elem.text.strip() if album_elem is not None and album_elem.text else ""
                    dur = 0.0
                    if dur_elem is not None and dur_elem.text:
                        try:
                            dur = max(0.0, float(dur_elem.text.strip()) / 1000.0)
                        except ValueError:
                            dur = 0.0

                    if is_cue_file(entry_path) and os.path.isfile(entry_path):
                        items.extend(parse_cue(entry_path))
                    elif is_playlist_file(entry_path) and os.path.isfile(entry_path):
                        items.extend(parse_playlist_file(entry_path, visited=visited))
                    elif os.path.isfile(entry_path) or is_audio_file(entry_path) or entry_path.startswith(("http://", "https://")):
                        if title or creator or album or dur > 0.0:
                            items.append(TrackInfo(
                                path=entry_path,
                                title=title,
                                artist=creator,
                                album=album,
                                duration=dur,
                            ))
                        else:
                            items.append(entry_path)
    except Exception:
        pass

    return items


def parse_playlist_file(path: str, visited: Optional[Set[str]] = None) -> List[Union[str, TrackInfo]]:
    """
    Parse a playlist or CUE sheet by detecting its extension.
    """
    abs_path = normalize_file_path(path)
    ext = os.path.splitext(abs_path)[1].lower()
    if ext == ".cue":
        return parse_cue(abs_path)
    elif ext in (".m3u", ".m3u8"):
        return parse_m3u(abs_path, visited=visited)
    elif ext == ".pls":
        return parse_pls(abs_path, visited=visited)
    elif ext == ".xspf":
        return parse_xspf(abs_path, visited=visited)
    return []


def _item_path(p: Union[str, TrackInfo]) -> str:
    """Return the filesystem path for a string or TrackInfo item."""
    return p.path if isinstance(p, TrackInfo) else p


def sort_audio_files(
    audio_files: List[Union[str, TrackInfo]],
    sort_method: int,
    folder: Optional[str] = None
) -> List[Union[str, TrackInfo]]:
    """Sort audio files/tracks by filename, title, disc & track number, or artist & title."""
    if sort_method == 0:
        if folder:
            def rel_sort_key(p: Union[str, TrackInfo]) -> tuple:
                raw_p = _item_path(p)
                rel = os.path.relpath(raw_p, folder)
                track_n = p.track_number if isinstance(p, TrackInfo) and p.track_number is not None else 0
                return ([natural_sort_key(part) for part in rel.split(os.sep)], track_n)
            audio_files.sort(key=rel_sort_key)
        else:
            def file_sort_key(p: Union[str, TrackInfo]) -> tuple:
                if isinstance(p, TrackInfo) and p.cue_path:
                    cue_base = os.path.basename(p.cue_path)
                    return (natural_sort_key(cue_base), p.track_number or 0)
                raw_p = _item_path(p)
                track_n = p.track_number if isinstance(p, TrackInfo) and p.track_number is not None else 0
                return (natural_sort_key(os.path.basename(raw_p)), track_n)
            audio_files.sort(key=file_sort_key)
    else:
        from player import _probe_metadata
        metadata_map: dict[str, dict] = {}
        for p in audio_files:
            if isinstance(p, TrackInfo):
                continue
            meta = _probe_metadata(p) or {}
            metadata_map[p] = meta

        def meta_sort_key(p: Union[str, TrackInfo]) -> tuple:
            if isinstance(p, TrackInfo):
                title = p.title or os.path.basename(p.path)
                artist = p.artist or ""
                disc = p.disc_number or 1
                track = p.track_number or 0
                fname = os.path.basename(p.path)
            else:
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
    return is_audio_file(filename)


def scan_audio_files_single_folder(folder: str, sort_method: int = 0) -> List[Union[str, TrackInfo]]:
    """
    Non-recursive scan of a single folder for supported audio files and CUE sheets.
    If CUE sheets are present, their tracks replace the backing image file.
    """
    audio_files: List[str] = []
    cue_files: List[str] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_file():
                    if is_audio_file(entry.name):
                        audio_files.append(entry.path)
                    elif is_cue_file(entry.name):
                        cue_files.append(entry.path)
    except OSError:
        pass

    results: List[Union[str, TrackInfo]] = []
    referenced_files: Set[str] = set()

    for c in cue_files:
        tracks = parse_cue(c)
        if tracks:
            results.extend(tracks)
            for t in tracks:
                referenced_files.add(t.path)

    for a in audio_files:
        if a not in referenced_files:
            results.append(a)

    return sort_audio_files(results, sort_method, folder=None)


def scan_audio_files_recursive(folder: str, sort_method: int = 0) -> List[Union[str, TrackInfo]]:
    """
    Recursively scans folder and all subdirectories for supported audio files and CUE sheets.
    """
    results: List[Union[str, TrackInfo]] = []
    try:
        for root_dir, dirs, files in os.walk(folder, followlinks=True):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            cue_files = [os.path.join(root_dir, f) for f in files if is_cue_file(f)]
            audio_files = [os.path.join(root_dir, f) for f in files if is_audio_file(f)]

            referenced_files: Set[str] = set()
            for c in cue_files:
                tracks = parse_cue(c)
                if tracks:
                    results.extend(tracks)
                    for t in tracks:
                        referenced_files.add(t.path)

            for a in audio_files:
                if a not in referenced_files:
                    results.append(a)
    except OSError:
        pass

    return sort_audio_files(results, sort_method, folder=folder)


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

    from player import _probe_metadata

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
