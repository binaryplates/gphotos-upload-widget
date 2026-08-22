#!/usr/bin/env bash
# User-level backend install for Photos Upload. Invoked from extension enable().
set -euo pipefail

EXT_ROOT="${1:?extension root required}"
BACKEND="$EXT_ROOT/backend"
HOME_DIR="${HOME:?}"
VENV="$HOME_DIR/.local/share/gphotos-upload-widget/venv"
STAMP="$HOME_DIR/.local/share/gphotos-upload-widget/setup-stamp"
DBUS_DIR="$HOME_DIR/.local/share/dbus-1/services"
SYSTEMD_DIR="$HOME_DIR/.config/systemd/user"
VERSION="$(python3 -c "import json; print(json.load(open('$EXT_ROOT/metadata.json'))['version'])")"

if [[ ! -f "$BACKEND/pyproject.toml" ]]; then
  echo "backend missing under $EXT_ROOT" >&2
  exit 2
fi

if [[ -f "$STAMP" ]] && [[ "$(cat "$STAMP")" == "$(printf '%s\n%s\n' "$EXT_ROOT" "$VERSION")" ]]; then
  exit 0
fi

python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install --upgrade "$BACKEND"

mkdir -p "$DBUS_DIR" "$SYSTEMD_DIR"
cat > "$DBUS_DIR/net.blazorplate.GPhotosUpload.service" <<EOF
[D-BUS Service]
Name=net.blazorplate.GPhotosUpload
Exec=$VENV/bin/gphotos-upload-service
EOF

cat > "$SYSTEMD_DIR/rclone-gphotos.service" <<EOF
[Unit]
Description=Selected folder upload to Google Photos
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BACKEND
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 $BACKEND/gphotos_upload_worker.py
Restart=on-failure
RestartSec=120
TimeoutStopSec=45
KillMode=mixed

[Install]
WantedBy=default.target
EOF

cat > "$SYSTEMD_DIR/gphotos-datefix.service" <<EOF
[Unit]
Description=Repair creation dates of media already in Google Photos
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BACKEND
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 $BACKEND/gphotos_datefix_worker.py
Restart=no
TimeoutStopSec=45
KillMode=mixed
EOF

systemctl --user daemon-reload
systemctl --user enable rclone-gphotos.service >/dev/null

nohup "$VENV/bin/gphotos-upload-service" >/dev/null 2>&1 &

printf '%s\n%s\n' "$EXT_ROOT" "$VERSION" > "$STAMP"
