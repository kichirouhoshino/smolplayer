# smolplayer

`smolplayer` is a free and open source music player that has no GUI whatsoever, leveraging the power of MPRIS.

Play a song in your file manager, and it will play completely in the background. As long as your desktop has it well-implemented, you can play, pause, go to the next track, previous track, shuffle, repeat, seek, and even control its volume, entirely in your media controller widget.

It uses your system's existing software libraries to play songs, meaning `smolplayer` is quite literally small. All you need is python, making it completely cross-platform.

## Features

- **No GUI** (yeah, that's a feature)
- **Full support for MPRIS** (Playback controls, album art, seeking, volume, shuffle, loop status)
- **Uses the folder where the song is located as the playlist**
- **Uses your system's ffmpeg or gstreamer for decoding**, meaning it can play virtually any audio codec you throw at it
- **Uses pw-cat to play songs**, meaning PipeWire plays raw audio with zero post-processing

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

## Planned features
- ReplayGain support
- Configuration file (for the features below)
- Ability to change shuffle algorithm
- Ability to change song sorting method

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
