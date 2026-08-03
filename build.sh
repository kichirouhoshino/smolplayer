#!/usr/bin/env bash
set -euo pipefail

# Change directory to the project root
cd "$(dirname "$0")"

# Check if flatpak is installed
if ! command -v flatpak &> /dev/null; then
    echo "Error: flatpak is not installed on this system."
    exit 1
fi

# Resolve Flatpak Manifest (supports JSON and YAML, filtering by app-id)
MANIFEST=$(find . -maxdepth 2 \( -name "*.yaml" -o -name "*.yml" -o -name "*.json" \) -exec grep -qE '^\s*["'\''\s]?(app-id|id)["'\''\s]?\s*:' {} \; -print -quit)
[[ -n "$MANIFEST" ]] || { echo "Error: No manifest found"; exit 1; }

# Parse App ID cleanly (handles indentation, quotes, colons, and commas)
APP_ID=$(grep -E '^\s*["'\''\s]?(app-id|id)["'\''\s]?\s*:' "$MANIFEST" | tr -d '"'\'' ' | cut -d':' -f2- | tr -d ',')
[[ -n "$APP_ID" ]] || { echo "Error: Could not find app-id"; exit 1; }

echo "Found manifest: $MANIFEST"
echo "Found App ID: $APP_ID"

REPO_DIR="$HOME/.flatpak-repo"
flatpak-builder --force-clean --repo="$REPO_DIR" --ccache --compose-url-policy=full build-dir "$MANIFEST"
flatpak build-update-repo "$REPO_DIR"

# Install Flatpak repository and application system-wide using run0 (single password prompt)
echo "Installing system-wide Flatpak repository and application using run0..."
run0 sh -c '
    flatpak remote-add --if-not-exists --no-gpg-verify local-system "file://$1"
    flatpak remote-modify --url="file://$1" local-system
    flatpak install -y --reinstall local-system "$2"
' _ "$REPO_DIR" "$APP_ID"
