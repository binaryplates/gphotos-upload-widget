#!/usr/bin/env bash
# One-command setup: install the GNOME Shell extension, the pip-installed
# D-Bus service it talks to, and the systemd user upload service — then
# enable everything. No sudo needed anywhere (unlike cpu-turbo) — every
# operation here (systemctl --user, rclone, pip --user) already runs
# unprivileged.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
UUID="gphotos-upload@blazorplate.net"
EXT_SRC="$REPO_ROOT/$UUID"
EXT_DEST="$HOME/.local/share/gnome-shell/extensions/$UUID"

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user, not root/sudo." >&2
  exit 1
fi

echo "== Step 1/4: installing the upload backend =="
"$SCRIPT_DIR/install-backend.sh"

echo "== Step 2/4: installing extension files =="
rm -rf "$EXT_DEST"
mkdir -p "$(dirname "$EXT_DEST")"
cp -a "$EXT_SRC" "$EXT_DEST"
glib-compile-schemas "$EXT_DEST/schemas"

echo "== Step 3/4: enabling extension =="
if ! gnome-extensions enable "$UUID" 2>/dev/null; then
  echo "Could not enable automatically — log out and back in once, then run:"
  echo "  gnome-extensions enable $UUID"
  exit 0
fi

echo "Done. Click the Photos Upload icon in your panel."
