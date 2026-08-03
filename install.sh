#!/usr/bin/env bash
set -euo pipefail

# Project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Target installation directories (User local, XDG compliant)
PREFIX="${HOME}/.local"
BIN_DIR="${PREFIX}/bin"
APP_DIR="${PREFIX}/share/smolplayer"
DESKTOP_DIR="${PREFIX}/share/applications"
METAINFO_DIR="${PREFIX}/share/metainfo"
ICON_BASE="${PREFIX}/share/icons/hicolor"

APP_ID="io.github.roddy.SmolPlayer"
DESKTOP_FILE="${DESKTOP_DIR}/${APP_ID}.desktop"
METAINFO_FILE="${METAINFO_DIR}/${APP_ID}.metainfo.xml"
WRAPPER_BIN="${BIN_DIR}/smolplayer"

do_uninstall() {
    echo "Uninstalling smolplayer (Local User Install)..."

    rm -rf "$APP_DIR"
    rm -f "$WRAPPER_BIN"
    rm -f "$DESKTOP_FILE"
    rm -f "$METAINFO_FILE"

    # Remove icons
    rm -f "${ICON_BASE}/scalable/apps/${APP_ID}.svg"
    rm -f "${ICON_BASE}/64x64/apps/${APP_ID}.png"
    rm -f "${ICON_BASE}/128x128/apps/${APP_ID}.png"
    rm -f "${ICON_BASE}/256x256/apps/${APP_ID}.png"
    rm -f "${ICON_BASE}/512x512/apps/${APP_ID}.png"

    # Update desktop & icon databases
    if command -v update-desktop-database &>/dev/null; then
        update-desktop-database "$DESKTOP_DIR" &>/dev/null || true
    fi

    if command -v gtk-update-icon-cache &>/dev/null; then
        gtk-update-icon-cache -q -t -f "$ICON_BASE" &>/dev/null || true
    fi

    echo "smolplayer has been successfully uninstalled!"
    exit 0
}

# Check for uninstall argument
if [[ "${1:-}" == "--uninstall" ]] || [[ "${1:-}" == "-u" ]]; then
    do_uninstall
fi

echo "Installing smolplayer locally to ${PREFIX}..."

# Create target directories
mkdir -p "$BIN_DIR" "$APP_DIR" "$DESKTOP_DIR" "$METAINFO_DIR"
mkdir -p "${ICON_BASE}/scalable/apps"
mkdir -p "${ICON_BASE}/64x64/apps"
mkdir -p "${ICON_BASE}/128x128/apps"
mkdir -p "${ICON_BASE}/256x256/apps"
mkdir -p "${ICON_BASE}/512x512/apps"

# Copy application python modules and assets
cp -f main.py player.py mpris.py playlist.py utils.py tray.py config.py test_smolplayer.py icon.svg icon-64.png icon-128.png icon-256.png icon-512.png "$APP_DIR/"
chmod +x "$APP_DIR/main.py"

# Create wrapper script in ~/.local/bin/smolplayer
cat << 'EOF' > "$WRAPPER_BIN"
#!/usr/bin/env bash
exec python3 "$HOME/.local/share/smolplayer/main.py" "$@"
EOF
chmod +x "$WRAPPER_BIN"

# Copy icons
cp -f icon.svg "${ICON_BASE}/scalable/apps/${APP_ID}.svg"
cp -f icon-64.png "${ICON_BASE}/64x64/apps/${APP_ID}.png"
cp -f icon-128.png "${ICON_BASE}/128x128/apps/${APP_ID}.png"
cp -f icon-256.png "${ICON_BASE}/256x256/apps/${APP_ID}.png"
cp -f icon-512.png "${ICON_BASE}/512x512/apps/${APP_ID}.png"

# Copy desktop entry and metainfo
cp -f io.github.roddy.SmolPlayer.desktop "$DESKTOP_FILE"
cp -f io.github.roddy.SmolPlayer.metainfo.xml "$METAINFO_FILE"

# Update desktop & icon databases
if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$DESKTOP_DIR" &>/dev/null || true
fi

if command -v gtk-update-icon-cache &>/dev/null; then
    gtk-update-icon-cache -q -t -f "$ICON_BASE" &>/dev/null || true
fi

echo "smolplayer successfully installed locally!"
echo "Installed to: $APP_DIR"
echo "Executable:   $WRAPPER_BIN"
echo "Desktop File: $DESKTOP_FILE"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo ""
    echo "Notice: $BIN_DIR is not in your \$PATH."
    echo "Add the following line to your ~/.bashrc or ~/.zshrc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
