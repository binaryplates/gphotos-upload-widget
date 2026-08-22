#!/usr/bin/env bash
# Simulate a fresh Extension Manager install on a clean user account.
set -euo pipefail

CPU_ZIP="${1:-/home/mohammad/RiderProjects/cpu-turbo-widget/dist/ego/cpu-turbo@blazorplate.net-v4.zip}"
GPH_ZIP="${2:-/home/mohammad/RiderProjects/gphotos-upload-widget/dist/ego/gphotos-upload@blazorplate.net-v6.zip}"

remove_enabled_uuid() {
  python3 - <<'PY' "$1"
import ast
import subprocess
import sys

uuid = sys.argv[1]
raw = subprocess.check_output(
    ["gsettings", "get", "org.gnome.shell", "enabled-extensions"],
    text=True,
).strip()
items = ast.literal_eval(raw.replace("'", '"'))
filtered = [item for item in items if item != uuid]
subprocess.run(
    [
        "gsettings",
        "set",
        "org.gnome.shell",
        "enabled-extensions",
        str(filtered).replace('"', "'"),
    ],
    check=True,
)
PY
}

echo "== Removing existing installs =="
systemctl --user stop rclone-gphotos.service gphotos-datefix.service 2>/dev/null || true
systemctl --user disable rclone-gphotos.service gphotos-datefix.service 2>/dev/null || true
pkill -f 'gphotos-upload-service' 2>/dev/null || true
gnome-extensions disable cpu-turbo@blazorplate.net gphotos-upload@blazorplate.net 2>/dev/null || true
gnome-extensions uninstall cpu-turbo@blazorplate.net 2>/dev/null || true
gnome-extensions uninstall gphotos-upload@blazorplate.net 2>/dev/null || true
remove_enabled_uuid cpu-turbo@blazorplate.net 2>/dev/null || true
remove_enabled_uuid gphotos-upload@blazorplate.net 2>/dev/null || true
rm -rf "$HOME/.local/share/gnome-shell/extensions/cpu-turbo@blazorplate.net"
rm -rf "$HOME/.local/share/gnome-shell/extensions/gphotos-upload@blazorplate.net"
sudo rm -f /usr/local/libexec/cpu-turbo-helper /usr/share/polkit-1/actions/net.blazorplate.cputurbo.policy
rm -f "$HOME/.local/share/dbus-1/services/net.blazorplate.GPhotosUpload.service"
rm -f "$HOME/.config/systemd/user/rclone-gphotos.service" "$HOME/.config/systemd/user/gphotos-datefix.service"
rm -f "$HOME/.local/share/gphotos-upload-widget/setup-stamp"
rm -rf "$HOME/.local/share/gphotos-upload-widget/venv"
systemctl --user daemon-reload 2>/dev/null || true

echo "== Checking EGO zip layouts =="
unzip -l "$GPH_ZIP" | rg -q 'backend/pyproject.toml'
unzip -l "$GPH_ZIP" | rg -q 'backend/run-setup.sh'
unzip -l "$CPU_ZIP" | rg -q 'backend/install-system-helper'
echo "OK zip layouts"

echo "== Installing from EGO zips =="
gnome-extensions install --force "$CPU_ZIP"
gnome-extensions install --force "$GPH_ZIP"

CPU_EXT="$HOME/.local/share/gnome-shell/extensions/cpu-turbo@blazorplate.net"
GPH_EXT="$HOME/.local/share/gnome-shell/extensions/gphotos-upload@blazorplate.net"
test -f "$CPU_EXT/metadata.json"
test -f "$GPH_EXT/metadata.json"
echo "OK extension files on disk"

echo "== Running bundled setup (same as extension enable) =="
bash "$GPH_EXT/backend/run-setup.sh" "$GPH_EXT"

echo "== Enabling extensions =="
ENABLE_OK=1
if ! gnome-extensions enable cpu-turbo@blazorplate.net 2>/dev/null; then
  ENABLE_OK=0
fi
if ! gnome-extensions enable gphotos-upload@blazorplate.net 2>/dev/null; then
  ENABLE_OK=0
fi

echo "== Verification =="
test -x "$HOME/.local/share/gphotos-upload-widget/venv/bin/gphotos-upload-service" && echo "OK gphotos service"
test -f "$HOME/.local/share/gphotos-upload-widget/setup-stamp" && echo "OK gphotos stamp"
test -f "$HOME/.local/share/dbus-1/services/net.blazorplate.GPhotosUpload.service" && echo "OK gphotos dbus"
test -f "$HOME/.config/systemd/user/rclone-gphotos.service" && echo "OK gphotos systemd"
test -x "$CPU_EXT/backend/install-system-helper" && echo "OK cpu installer"
if test -x /usr/local/libexec/cpu-turbo-helper; then
  echo "NOTE cpu helper already installed"
else
  echo "OK cpu awaits one-time pkexec"
fi

if [[ "$ENABLE_OK" -eq 1 ]]; then
  echo "OK extensions enabled in this session"
else
  echo "NOTE log out and back in (or restart GNOME Shell), then enable both extensions once."
  echo "  gnome-extensions enable cpu-turbo@blazorplate.net gphotos-upload@blazorplate.net"
fi

echo "Done. Toggle CPU turbo once to complete CPU system setup (pkexec prompt)."
