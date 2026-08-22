# Photos Upload

A native GNOME Shell extension: a panel indicator for toggling folder
uploads to Google Photos (via `rclone`), watching live progress/speed, and
checking Drive/API quota — all from one dropdown.

## Install

**Not one click.** You need the extension (EGO / Extension Manager) **and** a one-time backend install from GitHub.

→ **[INSTALL.md](INSTALL.md)** — step-by-step for end users (includes rclone setup)

Quick summary:

1. Install **Photos Upload** from Extension Manager or [extensions.gnome.org](https://extensions.gnome.org)
2. Run once in a terminal:

```bash
git clone https://github.com/binaryplates/gphotos-upload-widget.git
cd gphotos-upload-widget
./scripts/install-backend.sh
```

Developers: `./scripts/install.sh` installs backend + extension from a git checkout.

On Wayland, a brand-new extension may need one log out/in before the panel icon appears.

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
  gphotos_datefix.py        pure date-parsing/decision helpers (unit-tested)
  gphotos_datefix_worker.py the scan/apply date repair job, its own unit
schemas/        GSettings schema (refresh interval)
scripts/        install.sh, the systemd unit template
snap/           legacy tray Snap packaging (discontinued; channels closed)
```

## Fix dates

Google reads a photo's date from its embedded EXIF/QuickTime metadata **at
upload time**. Files that arrive without a usable date tag — WhatsApp images,
screenshots, re-encoded video — get dated to the moment they were uploaded
instead of when they were taken. The **Fix dates** chip in the dropdown finds
those and repairs them.

Click it once to scan: it walks every album this client created through the
Library API, works out each item's real date from its filename, and shows
what it would change. Nothing is touched yet. Click **Apply** to commit —
each item is downloaded to a temporary folder, checked against the metadata
it actually carries, re-tagged with `exiftool`, uploaded back **to the album
it came from**, and the original is moved aside into a cleanup album.

This is independent of your configured upload folders: it repairs what is
already in Google Photos, so it needs no local copy of anything and no source
folder set up.

Three limits are worth understanding before you use it:

- **A date cannot be changed in place.** `mediaItems.patch` accepts only
  `description`; there is no writable field for `creationTime`. Fixing
  therefore means re-uploading a corrected copy.
- **Every fix leaves a duplicate.** Re-tagging changes the file's content
  hash, so Google's dedupe does not catch it, and the Library API has no
  `mediaItems.delete` at any scope — nothing can delete the original for you.
  So the next best thing happens instead: each superseded original is moved
  into an album called **“Date fix — originals to delete”** and taken out of
  the album it was in. When the run finishes, **Open album** takes you
  straight there — select all, delete, done. Without that they would be
  scattered through a timeline of thousands.
- **Only media this widget uploaded is visible.** Since March 2025 the API
  grants `photoslibrary.readonly.appcreateddata`, so anything backed up by the
  phone's Google Photos app cannot be seen or fixed here. Scan is album-only
  for the same reason: `mediaItems.list` mostly returns empty pages while
  still paging the whole library, which made "Scanning items outside
  albums…" crawl for minutes without finding anything useful. rclone uploads
  land in albums, so album search is the complete set.

Runs are capped at `datefix_max_items` (500 by default) so a first pass cannot
exhaust the 10,000/day API quota, and a date fix never runs while an upload is
in progress — the two share a lock.

## Config

- `refresh-seconds` (GSettings) — panel refresh interval.
- `datefix_max_items` (config.json) — cap on items repaired per Fix dates run.
- `~/.config/gphotos-upload-widget/config.json` — source folders → album
  mappings, managed via the extension's Settings… window (never edit by
  hand while the extension is running).
- `~/.config/rclone/rclone.conf` — rclone's own remotes/credentials.
