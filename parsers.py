"""
parsers.py – Playlist parsers (.m3u, .pls, .xspf), CUE sheet parser (.cue), and directory scanners.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from typing import Any, List, Optional, Set, Union
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
from probing import TrackInfo, _probe_metadata


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
        try:
            import player
            probe_meta = getattr(player, "_probe_metadata", _probe_metadata)
        except Exception:
            probe_meta = _probe_metadata

        metadata_map: dict[str, dict] = {}
        for p in audio_files:
            if isinstance(p, TrackInfo):
                continue
            meta = probe_meta(p) or {}
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
