# smolplayer

`smolplayer` is a free and open source music player that has no GUI whatsoever, leveraging the power of MPRIS.

Play a song in your file manager, and it will play completely in the background. As long as your desktop has it well-implemented, you can play, pause, go to the next track, previous track, shuffle, repeat, seek, and even control its volume, entirely in your media controller widget.

It uses your system's existing software libraries to play songs, meaning `smolplayer` is quite literally small. All you need is python, making it completely cross-platform.

This is still in development but you can pretty much use it. Just needs a few more bug squashing.

## Features

- **No GUI** (yeah, that's a feature)
- **Full support for MPRIS** (Playback controls, album art, seeking, volume, shuffle, loop status)
- **Uses the folder where the song is located as the playlist**
- Support for **playlist** and **cue** files
- **Uses your system's ffmpeg or gstreamer for decoding**, meaning it can play virtually any audio codec you throw at it
- **Uses pw-cat to play songs**, meaning PipeWire plays raw audio with zero post-processing
- **Optional Bit-Perfect Playback** through direct ALSA
- **ReplayGain Support** (disabled by default)
- **Config** for setting shuffle algo, sorting order, and others

## Installation & Building

### Requirements 
- python
- python-dbus
- Any form of multimedia controller that uses MPRIS (Heavily tested on KDE Plasma and Gnome)
- ffmpeg or gstreamer (ffmpeg is used first, fallbacks to gstreamer if it cannot decode a format)
- PipeWire Audio Server

### Flatpak
If you already use flatpaks for your system, the flatpak version of `smolplayer` is highly recommended. The freedesktop runtime has a fully featured ffmpeg that should play almost anything. Note that it has access to all files to make it easier to play songs from any location.

```bash
flatpak-builder --user --install --force-clean build-dir io.github.roddy.SmolPlayer.yaml
```

### Manual Install
If you're concerned about the storage implications flatpak brings, you can also install `smolplayer` on your user folder.

```bash
chmod +x install.sh
./install.sh
```

To uninstall `smolplayer`:

```bash
./install.sh --uninstall
```

## MPRIS Control

Control playback with your preferred media controller widget or command line utilities like `playerctl`:

```bash
playerctl -p smolplayer play-pause
playerctl -p smolplayer next
playerctl -p smolplayer previous
playerctl -p smolplayer volume 0.8
```

## Configuration

Configuration options can be edited in `~/.config/smolplayer/config.ini` or opened with:

```bash
smolplayer --config
```

| Option | Description & Values | Default |
| :--- | :--- | :--- |
| `replay_gain` | `0` = Off<br>`1` = Use Track Gain<br>`2` = Use Album Gain | `0` |
| `shuffle_algo` | `0` = Fisher-Yates<br>`1` = Smart Shuffle (Artist-spaced)<br>`2` = True Random | `0` |
| `sort_method` | `0` = By filename<br>`1` = By title<br>`2` = By disc and track number<br>`3` = By artist & title | `0` |
| `recurse_fileopen` | `0` = Single parent folder only<br>`1` = Recursive subfolders on file open | `0` |
| `recurse_folderopen` | `0` = Top-level folder only<br>`1` = Recursive subfolders on folder open | `1` |
| `timeout` | Auto-close after being paused for N minutes (`0` = disabled) | `30` |
| `presence` | `0` = Tray and Notifications enabled<br>`1` = Tray only<br>`2` = Notifications only<br>`3` = All off | `0` |
| `remember_toggles` | `0` = False<br>`1` = Remember Shuffle and Repeat states across sessions | `0` |
| `decode_method` | `0` = FFmpeg (recommended)<br>`1` = GStreamer (automatic fallback if unavailable) | `0` |
| `replaygain_preamp` | Pre-amp gain for songs with ReplayGain info (in dB) | `0` |
| `replaygain_default_preamp` | Pre-amp gain for songs without ReplayGain info (in dB) | `0` |
| `replaygain_peaking` | `0` = Disabled<br>`1` = Apply appropriate gain<br>`2` = Dynamic range compression if clipping | `0` |
| `internal_resampler` | `0` = Disabled (PipeWire resamples)<br>`1` = FFmpeg/GStreamer resamples to sink | `0` |
| `bit_perfect` | `0` = Disabled (PipeWire)<br>`1` = Enabled (Direct ALSA hardware output) | `0` |
| `cue_noshuffle` | `0` = False<br>`1` = Force shuffle off when loading `.cue` files | `1` |
| `cue_order` | `0` = False (Preserve playlist/CUE order)<br>`1` = True (Apply `sort_method` to playlists/CUE) | `0` |
| `lossy_bps` | `0` = Default (usually 32-bit float)<br>`1` = 16-bit integer PCM for lossy sources | `0` |

## License

GPL-3.0-or-later

smolplayer
Copyright (C) 2026 roddy

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
