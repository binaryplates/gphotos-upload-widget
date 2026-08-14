# Photos Upload

A native GNOME Shell extension: a panel indicator for toggling folder
uploads to Google Photos (via `rclone`), watching live progress/speed, and
checking Drive/API quota — all from one dropdown.

## Install

```bash
scripts/install.sh
```

This installs the extension to
`~/.local/share/gnome-shell/extensions/gphotos-upload@blazorplate.net`,
installs the background D-Bus service the extension talks to, installs the
`rclone-gphotos.service` systemd `--user` unit that actually runs the
uploads, and enables the extension. On Wayland, a brand-new extension needs
one log out/in before GNOME Shell picks it up for the first time.

You'll also need `rclone` configured with a `gphotos:` (Google Photos) and
optionally a `gdrive:` (Drive quota display) remote — the extension's
Settings… → Google credentials page can set the OAuth client ID/secret, but
the initial OAuth consent flow itself is `rclone`'s, done once via
`rclone config`.

## Architecture

The extension never shells out per action. It talks over D-Bus to a small,
pip-installed service (`backend/gphotos_upload_service/`) that owns
`net.blazorplate.GPhotosUpload` on the session bus and does the actual
`rclone`/credential/quota work; the real file transfer itself runs in a
separate systemd `--user` unit (`rclone-gphotos.service`) so it keeps going
even if the panel dropdown is closed.

```
lib/            PanelMenu.Button + PopupMenu UI (GJS), talks to the D-Bus
                service via Gio.DBusProxy
backend/
  gphotos_upload_service/   pip-installable D-Bus service
  gphotos_upload_common.py  shared config/validation helpers (unchanged)
  gphotos_upload_worker.py  the actual upload loop, run by the systemd unit
schemas/        GSettings schema (refresh interval)
scripts/        install.sh, the systemd unit template
snap/           legacy Snap Store packaging (superseded by this extension,
                kept only because the old Snap Store listing still exists)
```

## Config

- `refresh-seconds` (GSettings) — panel refresh interval.
- `~/.config/gphotos-upload-widget/config.json` — source folders → album
  mappings, managed via the extension's Settings… window (never edit by
  hand while the extension is running).
- `~/.config/rclone/rclone.conf` — rclone's own remotes/credentials.
