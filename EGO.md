# extensions.gnome.org submission

Build zip: `./scripts/ego-pack.sh` → `dist/ego/<uuid>-v<version>.zip`

Upload at https://extensions.gnome.org/upload

## Dependency (not in the EGO zip)

The extension package does **not** include `backend/`. Install the upload service once from GitHub:

```bash
git clone https://github.com/binaryplates/gphotos-upload-widget.git
cd gphotos-upload-widget
./scripts/install-backend.sh
```

This pip-installs the D-Bus service, writes user systemd units, and starts the upload worker.

## Review reply (74277)

Removed `backend/` from the extension zip. The Python/D-Bus backend is installed separately via `scripts/install-backend.sh` in the GitHub repo linked from metadata `url`. The extension checks for the installed service and notifies if setup is missing.
