# extensions.gnome.org submission

Build zip: `./scripts/ego-pack.sh` → `dist/ego/<uuid>-v<version>.zip`

Upload at https://extensions.gnome.org/upload

## End-user install docs

Point users to **[INSTALL.md](INSTALL.md)** in the repo (linked from EGO via metadata `url`).

EGO description should mention: extension first, then `scripts/install-backend.sh` from GitHub, plus rclone.

## Review reply (74277)

Removed `backend/` from the extension zip. The Python/D-Bus backend is installed separately via `scripts/install-backend.sh` in the GitHub repo linked from metadata `url`. The extension checks for the installed service and notifies if setup is missing.
