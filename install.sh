#!/usr/bin/env bash
# notion-iwe-sync installer
#
# Installs the CLI from this checkout (uv or pipx), creates a config template
# if none exists, and installs the systemd user unit for watch mode.
#
#   ./install.sh            install CLI + config template + systemd unit
#   ./install.sh --enable   same, and enable + start the watch daemon now
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$HOME/.config/notion-iwe"
CONFIG="$CONFIG_DIR/config.toml"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/notion-iwe-sync.service"

# --- 1. install the CLI ---
if command -v uv >/dev/null 2>&1; then
  echo "installing with uv..."
  uv tool install --force -e "$HERE"
elif command -v pipx >/dev/null 2>&1; then
  echo "installing with pipx..."
  pipx install --force -e "$HERE"
else
  echo "error: need either 'uv' or 'pipx' on PATH to install." >&2
  echo "  uv:   curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  echo "  pipx: python3 -m pip install --user pipx" >&2
  exit 1
fi

# --- 2. config template (never overwrite an existing config) ---
if [ ! -f "$CONFIG" ]; then
  mkdir -p "$CONFIG_DIR"
  cat > "$CONFIG" <<'EOF'
token = "ntn_REPLACE_ME"       # Notion internal-integration secret
vault = "/home/REPLACE_ME/notion"  # where the markdown mirror lives
pull_interval = 300            # seconds between pulls in watch mode
new_page_parent = ""           # Notion page id for NEW local .md files; empty = skip
EOF
  chmod 600 "$CONFIG"
  echo "created config template: $CONFIG  (edit it before first run!)"
else
  echo "config already exists: $CONFIG  (left untouched)"
fi

# --- 3. systemd user unit ---
mkdir -p "$UNIT_DIR"
cp "$HERE/contrib/notion-iwe-sync.service" "$UNIT"
systemctl --user daemon-reload
echo "installed systemd unit: $UNIT"

if [ "${1:-}" = "--enable" ]; then
  systemctl --user enable --now notion-iwe-sync
  echo "watch daemon enabled and started."
else
  echo
  echo "next steps:"
  echo "  1. edit $CONFIG (token + vault)"
  echo "  2. notion-iwe pull                            # first mirror"
  echo "  3. systemctl --user enable --now notion-iwe-sync   # start the daemon"
fi
