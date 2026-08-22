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
SERVICE_DEST="$HOME/.config/systemd/user/rclone-gphotos.service"
DATEFIX_DEST="$HOME/.config/systemd/user/gphotos-datefix.service"
DBUS_SERVICES_DIR="$HOME/.local/share/dbus-1/services"

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user, not root/sudo." >&2
  exit 1
fi

echo "== Step 1/4: installing extension files =="
rm -rf "$EXT_DEST"
mkdir -p "$(dirname "$EXT_DEST")"
cp -a "$EXT_SRC" "$EXT_DEST"
cp -a "$REPO_ROOT/backend" "$EXT_DEST/backend"
glib-compile-schemas "$EXT_DEST/schemas"

echo "== Step 2/4: installing the gphotos-upload-service D-Bus service =="
VENV_DIR="$REPO_ROOT/venv"
python3 -m venv --system-site-packages "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade "$REPO_ROOT/backend"
mkdir -p "$DBUS_SERVICES_DIR"
sed "s#__VENV_DIR__#$VENV_DIR#g" "$REPO_ROOT/backend/net.blazorplate.GPhotosUpload.service" > "$DBUS_SERVICES_DIR/net.blazorplate.GPhotosUpload.service"

echo "== Step 3/4: installing the systemd user services =="
mkdir -p "$(dirname "$SERVICE_DEST")"
sed "s#__APP_DIR__#$REPO_ROOT#g" "$SCRIPT_DIR/rclone-gphotos.service" > "$SERVICE_DEST"
sed "s#__APP_DIR__#$REPO_ROOT#g" "$SCRIPT_DIR/gphotos-datefix.service" > "$DATEFIX_DEST"
systemctl --user daemon-reload
systemctl --user enable rclone-gphotos.service >/dev/null

echo "== Step 4/4: enabling extension =="
if ! gnome-extensions enable "$UUID" 2>/dev/null; then
  echo "Could not enable automatically — log out and back in once, then run:"
  echo "  gnome-extensions enable $UUID"
  exit 0
fi

echo "Done. Click the Photos Upload icon in your panel."
