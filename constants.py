"""
constants.py – Application-wide constants for smolplayer.

Single source of truth for all app identifiers, D-Bus names, paths,
and tuneable numeric defaults. No other module should hardcode these values.
"""

# ---------------------------------------------------------------------------
# Application identity
# ---------------------------------------------------------------------------

APP_NAME        = "smolplayer"
APP_ID          = "io.github.roddy.SmolPlayer"
APP_DISPLAY     = "smolplayer"   # Human-readable name shown in UIs
APP_VERSION     = "0.1.0"

# ---------------------------------------------------------------------------
# D-Bus / MPRIS names
# ---------------------------------------------------------------------------

MPRIS_BUS_NAME  = f"org.mpris.MediaPlayer2.{APP_NAME}"
MPRIS_OBJ_PATH  = "/org/mpris/MediaPlayer2"
MPRIS_IFACE     = "org.mpris.MediaPlayer2"
MPRIS_PLAYER    = "org.mpris.MediaPlayer2.Player"
DBUS_PROPS      = "org.freedesktop.DBus.Properties"

TRACK_OBJ_PATH  = f"/org/{APP_NAME}/Track"   # prefix; suffixed with /<index>

# ---------------------------------------------------------------------------
# StatusNotifierItem (system tray)
# ---------------------------------------------------------------------------

SNI_IFACE       = "org.kde.StatusNotifierItem"
MENU_IFACE      = "com.canonical.dbusmenu"
WATCHER_BUS     = "org.kde.StatusNotifierWatcher"
WATCHER_PATH    = "/StatusNotifierWatcher"

SNI_OBJ_PATH    = "/StatusNotifierItem"
MENU_OBJ_PATH   = "/StatusNotifierMenu"

# ---------------------------------------------------------------------------
# PipeWire / pw-cat node properties
# ---------------------------------------------------------------------------

PWCAT_NODE_NAME        = APP_NAME          # node.name / media.name / app.name
PWCAT_NODE_DESCRIPTION = APP_NAME          # node.description
PWCAT_LATENCY          = "100ms"

# ---------------------------------------------------------------------------
# Audio pipeline defaults
# ---------------------------------------------------------------------------

DEFAULT_SAMPLE_RATE    = 44100
DEFAULT_CHANNELS       = 2
DEFAULT_SAMPLE_FMT     = "s16"
DEFAULT_FFMPEG_FMT     = "s16le"

# ffprobe / ffmpeg probe tuning
FFPROBE_ANALYZEDURATION = "100000"
FFPROBE_PROBESIZE       = "32768"

# Silence lead-in before the first audio frame (seconds)
PIPELINE_SILENCE_LEAD_IN = 0.040          # 40 ms

# Read chunk size for the pump loop (bytes)
PIPELINE_CHUNK_SIZE    = 65536

# How many bytes of silence to drain pw-cat on teardown
PWCAT_DRAIN_BYTES      = 2048

# ---------------------------------------------------------------------------
# Volume sync / pactl
# ---------------------------------------------------------------------------

VOLUME_SYNC_INTERVAL   = 0.20             # seconds between passive polls
VOLUME_SINK_RETRY_SECS = 0.5             # how long to retry finding sink-input

# ---------------------------------------------------------------------------
# Desktop notifications
# ---------------------------------------------------------------------------

NOTIFICATION_TIMEOUT_MS = 5000           # milliseconds
NOTIFICATION_ICON       = APP_ID         # icon name sent to libnotify / D-Bus

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

# Consecutive playback failures before giving up and notifying the user
MAX_CONSECUTIVE_FAILURES = 5
