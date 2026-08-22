#!/usr/bin/env python3
"""Repair the creation date of media already uploaded to Google Photos.

Runs as its own systemd --user unit, driven by a request file the D-Bus
service writes, and reports progress through datefix.status for the panel
to poll — the same shape as gphotos_upload_worker.py.

Two modes:

  scan   Read every item in the configured albums through the Library API
         and work out, from the filename, which ones Google dated wrongly.
         Touches nothing.

  apply  For each candidate from the scan: download the original, confirm
         against its real embedded metadata, rewrite the date tags with
         exiftool, upload the corrected file, and drop the original out of
         the album.

Why re-upload rather than edit in place: mediaItems.patch accepts only
`description`, so an item's creationTime cannot be changed once uploaded.
That also means every fix leaves a duplicate behind — the Library API has
no mediaItems.delete, so the wrong-date original survives in the user's
timeline and has to be trashed by hand. The counts we report make that
leftover work explicit rather than pretending it away.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from gphotos_datefix import (  # noqa: E402
    exiftool_args_for,
    exiftool_extension_for_format,
    is_misdated,
    is_video,
    parse_creation_time,
    parse_filename_date_with_precision,
    resolve_date,
    sniff_image_format_path,
    tolerance_for,
)
from gphotos_upload_common import (  # noqa: E402
    DATEFIX_LOG_PATH,
    DATEFIX_ORIGINALS_LOG_PATH,
    DATEFIX_REQUEST_PATH,
    DATEFIX_STATUS_PATH,
    PRIVATE_FILE_MODE,
    UPLOAD_LOCK_PATH,
    atomic_write_json,
    ensure_private_path,
    read_json,
    rotate_log,
)

sys.path.insert(0, str(APP_DIR))
from gphotos_upload_service import logic  # noqa: E402

# Where the superseded originals are gathered so they can be deleted in one
# pass. A fixed title means repeated runs reuse the same album.
CLEANUP_ALBUM = "Date fix — originals to delete"

DOWNLOAD_TIMEOUT = 120
EXIFTOOL_TIMEOUT = 60
RCLONE_TIMEOUT = 300

# Parallelism for Apply. Byte uploads may run concurrently; Google still
# serializes mediaItems.create under the hood (rclone hits that). Defaults
# are modest so a first run cannot stampede the API.
PREPARE_WORKERS = 4
UPLOAD_TRANSFERS = 4
APPLY_CHUNK = 8

_stop_requested = False


def _log(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}\n"
    try:
        with DATEFIX_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def _log_original(**fields) -> None:
    """Append one superseded original to the durable audit log.

    JSONL so each Apply run can be tailed/grepped without loading a huge
    array — the cleanup album is manual, and this file is the permanent
    record of which Google item ids were replaced.
    """
    record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
    try:
        ensure_private_path(DATEFIX_ORIGINALS_LOG_PATH)
        with DATEFIX_ORIGINALS_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        _log(f"could not write originals log: {exc}")


WORKDIR_PREFIX = "gphotos-datefix-"


def _clear_stale_workdirs() -> None:
    """Drop work dirs left behind when a run was killed mid-download.

    Safe because we hold the upload lock by the time this runs, so no other
    date-fix run can own one of these.
    """
    for stale in Path(tempfile.gettempdir()).glob(f"{WORKDIR_PREFIX}*"):
        shutil.rmtree(stale, ignore_errors=True)


def _acquire_lock():
    """Share the uploader's lock so the two can never run at once."""
    ensure_private_path(UPLOAD_LOCK_PATH.parent, is_dir=True)
    handle = UPLOAD_LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    os.set_inheritable(handle.fileno(), False)
    return handle


def _release_lock(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    except OSError:
        pass


def _write_status(**fields) -> None:
    fields.setdefault("updated_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    try:
        atomic_write_json(DATEFIX_STATUS_PATH, fields)
    except OSError as exc:
        _log(f"could not write status: {exc}")


def _safe_name(filename: str) -> str:
    """The item's own basename, made safe to write to disk.

    rclone names the uploaded item after the local file, so this has to
    stay as close to the original as possible — a mangled name here shows
    up as the filename in Google Photos. Each item gets its own directory
    instead, which keeps collisions impossible without touching the name.
    """
    name = Path(str(filename).replace("/", "_")).name.strip()
    if name in ("", ".", ".."):
        return "item.bin"
    if len(name) <= 120:
        return name
    # Trim the stem, never the suffix — exiftool and rclone both key off it.
    suffix = Path(name).suffix[:16]
    return Path(name).stem[:120 - len(suffix)] + suffix


def _download(url: str, destination: Path, *, video: bool) -> None:
    """Fetch the original bytes. '=d' keeps EXIF; bare baseUrl strips it."""
    suffix = "=dv" if video else "=d"
    req = urllib.request.Request(url + suffix)
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, destination.open("wb") as out:
        shutil.copyfileobj(resp, out)
    os.chmod(destination, PRIVATE_FILE_MODE)


def _read_tags(path: Path) -> dict:
    try:
        proc = subprocess.run(
            ["exiftool", "-j", "-api", "QuickTimeUTC=1", str(path)],
            capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log(f"exiftool read failed for {path.name}: {exc}")
        return {}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        import json as _json
        parsed = _json.loads(proc.stdout)
    except ValueError:
        return {}
    return parsed[0] if isinstance(parsed, list) and parsed else {}


def _write_date(path: Path, when: datetime, mime_type: str) -> bool:
    image_format = "" if is_video(mime_type, path.name) else sniff_image_format_path(path)
    write_path = path
    restore = False
    if image_format:
        wanted = exiftool_extension_for_format(image_format)
        if wanted and path.suffix.lower() != wanted:
            # exiftool keys off the extension; PNG bytes in a .jpg name fail
            # unless we briefly rename to match the real format.
            write_path = path.with_suffix(wanted)
            path.rename(write_path)
            restore = True
    args = [
        "exiftool", "-overwrite_original",
        *exiftool_args_for(mime_type, when, path.name, image_format=image_format),
        str(write_path),
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if restore:
            write_path.rename(path)
        _log(f"exiftool write failed for {path.name}: {exc}")
        return False
    if restore:
        write_path.rename(path)
    if proc.returncode != 0:
        _log(f"exiftool write failed for {path.name}: {(proc.stderr or proc.stdout).strip()}")
        return False
    return True


def _upload(path: Path, album: str) -> bool:
    """Put the corrected file back where the original lived.

    An item that was in an album goes back to that album; one that was
    loose in the library goes to gphotos:upload, which adds it to the
    library without filing it anywhere.
    """
    destination = f"gphotos:album/{album}" if album else "gphotos:upload"
    command = [
        "rclone", "copy", str(path), destination,
        "--transfers", "1", "--checkers", "1", "--retries", "3",
        "--low-level-retries", "10", "--log-level", "INFO",
        "--log-file", str(DATEFIX_LOG_PATH),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=RCLONE_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log(f"rclone upload failed for {path.name}: {exc}")
        return False
    if proc.returncode != 0:
        _log(f"rclone exited {proc.returncode} for {path.name}")
        return False
    return True


def _upload_many(paths: list[Path], album: str, *, transfers: int = 1) -> set[Path]:
    """Upload several prepared files; return which ones succeeded.

    transfers>1 copies through a staging directory so rclone can run
    concurrent byte uploads (Google's recommended step-1 parallelism).
    On a batch failure we fall back to one-by-one so a single bad file
    does not strand the rest.
    """
    if not paths:
        return set()
    if transfers <= 1 or len(paths) == 1:
        return {path for path in paths if _upload(path, album)}

    destination = f"gphotos:album/{album}" if album else "gphotos:upload"
    staging = Path(tempfile.mkdtemp(prefix="gphotos-datefix-up-"))
    try:
        staged: list[tuple[Path, Path]] = []
        for path in paths:
            dest = staging / path.name
            if dest.exists():
                dest = staging / f"{path.parent.name}_{path.name}"
            try:
                os.link(path, dest)
            except OSError:
                shutil.copy2(path, dest)
            staged.append((path, dest))
        command = [
            "rclone", "copy", str(staging), destination,
            "--transfers", str(max(1, transfers)),
            "--checkers", str(max(1, min(transfers, 8))),
            "--retries", "3", "--low-level-retries", "10",
            "--log-level", "INFO", "--log-file", str(DATEFIX_LOG_PATH),
        ]
        timeout = max(RCLONE_TIMEOUT, (RCLONE_TIMEOUT // 2) * len(paths))
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log(f"rclone batch upload failed for {album or 'library'}: {exc}")
            proc = None
        if proc is not None and proc.returncode == 0:
            return {path for path, _ in staged}
        _log(
            f"rclone batch exited {getattr(proc, 'returncode', '?')} for "
            f"{album or 'library'} ({len(paths)} files); falling back to serial"
        )
        return {path for path, _ in staged if _upload(path, album)}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _prepare_fix(
    candidate: dict,
    live: dict,
    item_dir: Path,
) -> dict:
    """Download + decide + retag one candidate. Does not upload.

    Returns a result dict with keys: filename, status ('ready'|'skipped'|'failed'),
    and on success also path/resolved/source/candidate.
    """
    name = candidate["filename"]
    if not live or not live.get("baseUrl"):
        return {"filename": name, "status": "failed", "result": "no longer in Google Photos"}

    item_dir.mkdir(parents=True, exist_ok=True)
    temp = item_dir / _safe_name(name)
    try:
        _download(live["baseUrl"], temp, video=is_video(candidate["mimeType"], name))
    except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"filename": name, "status": "failed", "result": f"download failed: {exc}"}

    tags = _read_tags(temp)
    creation = parse_creation_time(candidate["current"])
    resolved, source = resolve_date(filename=name, tags=tags, album=candidate["album"])
    if resolved is None:
        temp.unlink(missing_ok=True)
        return {"filename": name, "status": "skipped", "result": "no usable date"}

    _guess, precision = parse_filename_date_with_precision(name)
    if not is_misdated(creation, resolved, tolerance=tolerance_for(precision)):
        temp.unlink(missing_ok=True)
        return {
            "filename": name, "status": "skipped",
            "result": "already correct per embedded metadata",
        }
    if source.startswith("exif:"):
        _log(f"{name}: embedded {source} overrides filename guess {candidate['proposed']}")

    if not _write_date(temp, resolved, candidate["mimeType"]):
        temp.unlink(missing_ok=True)
        return {"filename": name, "status": "failed", "result": "could not write date tags"}

    return {
        "filename": name,
        "status": "ready",
        "path": temp,
        "resolved": resolved,
        "source": source,
        "candidate": candidate,
    }


# ------------------------------------------------------------------ scan ---

def _progress_status(**fields) -> None:
    """Mid-scan status without the full candidate list.

    Candidates grow to hundreds of KB; rewriting them on every progress tick
    dominated runtime on large albums. The panel only needs phase/counts
    until the scan finishes, when the full list is written once for Apply.
    """
    fields.pop("candidates", None)
    _write_status(**fields)


def run_scan(request: dict) -> int:
    token = logic._gphotos_access_token()
    if not token:
        _write_status(mode="scan", finished=True, ok=False,
                      error="No Google Photos access token — check Settings → Google credentials.")
        return 1

    # Albums come from the account, not from the configured upload folders:
    # this repairs what is already in Google Photos.
    try:
        albums = logic.list_albums(token)
    except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        _write_status(mode="scan", finished=True, ok=False, error=f"Could not list albums: {exc}")
        return 1

    candidates: list[dict] = []
    undetermined = 0
    scanned = 0
    seen: set[str] = set()
    # Albums here run to thousands of items; without periodic updates the
    # panel would sit on a frozen count for minutes at a time.
    progress_every = 100

    def triage(item: dict, album: str, album_id: str) -> None:
        nonlocal scanned, undetermined
        scanned += 1
        seen.add(item["id"])
        guess, precision = parse_filename_date_with_precision(item["filename"])
        if guess is None:
            undetermined += 1
            return
        if not is_misdated(parse_creation_time(item["creationTime"]), guess,
                           tolerance=tolerance_for(precision)):
            return
        candidates.append({
            "id": item["id"],
            "album": album,
            "album_id": album_id,
            "filename": item["filename"],
            "mimeType": item["mimeType"],
            "productUrl": item["productUrl"],
            "current": item["creationTime"],
            "proposed": guess.isoformat(timespec="seconds"),
            "source": "filename",
            "precision": precision,
        })

    for title, entry in albums.items():
        if _stop_requested:
            break
        if not entry.get("id"):
            continue
        album_total = int(entry.get("count") or 0)
        album_seen = 0
        _progress_status(mode="scan", finished=False, ok=True, scanned=scanned,
                         undetermined=undetermined, candidate_count=len(candidates),
                         phase=f"Scanning {title}…")
        try:
            pages = logic.iter_album_items(
                token, entry["id"], should_stop=lambda: _stop_requested,
            )
            for page in pages:
                if _stop_requested:
                    break
                if title == CLEANUP_ALBUM:
                    # Already-superseded originals — never re-fix them.
                    seen.update(item["id"] for item in page)
                    album_seen += len(page)
                    continue
                for item in page:
                    if _stop_requested:
                        break
                    triage(item, title, entry["id"])
                    album_seen += 1
                    if album_seen % progress_every == 0:
                        total_hint = f"/{album_total}" if album_total else ""
                        _progress_status(
                            mode="scan", finished=False, ok=True, scanned=scanned,
                            undetermined=undetermined, candidate_count=len(candidates),
                            phase=f"Scanning {title} ({album_seen}{total_hint})…",
                        )
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            _log(f"could not search album {title}: {exc}")
            continue
        if title != CLEANUP_ALBUM:
            _progress_status(mode="scan", finished=False, ok=True, scanned=scanned,
                             undetermined=undetermined, candidate_count=len(candidates),
                             phase=f"Scanning {title}…")

    # Deliberately no mediaItems.list "outside albums" pass. With the
    # appcreateddata scope that endpoint mostly returns empty pages while
    # still paging through the user's whole library (phone backups and
    # all) — measured at ~1s/page and majority-empty, so a scan could sit
    # on "Scanning items outside albums…" for many minutes and find almost
    # nothing new. rclone uploads go into albums; that is what we repair.

    _write_status(
        mode="scan", finished=True, ok=True, scanned=scanned,
        undetermined=undetermined, candidates=candidates,
        candidate_count=len(candidates),
        albums_scanned=len(albums),
        cancelled=_stop_requested,
        phase=f"{len(candidates)} mis-dated, {undetermined} undetermined, {scanned} scanned",
    )
    return 0


# ----------------------------------------------------------------- apply ---

def run_apply(request: dict) -> int:
    candidates = request.get("candidates") or []
    max_items = int(request.get("max_items") or 500)
    prepare_workers = max(1, int(request.get("prepare_workers") or PREPARE_WORKERS))
    upload_transfers = max(1, int(request.get("upload_transfers") or UPLOAD_TRANSFERS))
    chunk_size = max(1, int(request.get("chunk_size") or APPLY_CHUNK))
    batch = candidates[:max_items]
    token = logic._gphotos_access_token()
    if not token:
        _write_status(mode="apply", finished=True, ok=False,
                      error="No Google Photos access token — check Settings → Google credentials.")
        return 1

    # baseUrls from the scan have expired by now (they last about an hour),
    # so re-read each album to get fresh ones before downloading anything.
    fresh: dict[str, dict] = {}
    album_ids = {item["album_id"] for item in batch if item["album_id"]}
    for album_id in album_ids:
        try:
            for item in logic.search_album_items(token, album_id):
                fresh[item["id"]] = item
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            _log(f"could not refresh album {album_id}: {exc}")
    if any(not item["album_id"] for item in batch):
        try:
            for item in logic.list_library_items(token):
                fresh.setdefault(item["id"], item)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            _log(f"could not refresh library items: {exc}")

    fixed = 0
    skipped = 0
    failed = 0
    remove_by_album: dict[str, list[str]] = {}
    originals: list[dict] = []
    details: list[dict] = []
    workdir = Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX))
    done = 0

    try:
        for chunk_start in range(0, len(batch), chunk_size):
            if _stop_requested:
                break
            chunk = batch[chunk_start:chunk_start + chunk_size]
            _write_status(
                mode="apply", finished=False, ok=True, total=len(batch), done=done,
                fixed=fixed, skipped=skipped, failed=failed,
                phase=(
                    f"Preparing {chunk_start + 1}–{chunk_start + len(chunk)}/"
                    f"{len(batch)} ({prepare_workers} workers)…"
                ),
            )

            prepared: list[dict] = []
            with ThreadPoolExecutor(max_workers=prepare_workers) as pool:
                futures = {
                    pool.submit(
                        _prepare_fix,
                        candidate,
                        fresh.get(candidate["id"]) or {},
                        workdir / f"{chunk_start + offset:05d}",
                    ): candidate
                    for offset, candidate in enumerate(chunk, start=1)
                }
                for future in as_completed(futures):
                    if _stop_requested:
                        break
                    result = future.result()
                    status = result["status"]
                    if status == "ready":
                        prepared.append(result)
                    elif status == "skipped":
                        skipped += 1
                        details.append({"filename": result["filename"], "result": result["result"]})
                        done += 1
                    else:
                        failed += 1
                        details.append({"filename": result["filename"], "result": result["result"]})
                        done += 1

            # Upload by album so rclone destinations stay correct; concurrent
            # byte transfers within each album group.
            by_album: dict[str, list[dict]] = {}
            for result in prepared:
                by_album.setdefault(result["candidate"]["album"], []).append(result)

            for album, group in by_album.items():
                if _stop_requested:
                    break
                _write_status(
                    mode="apply", finished=False, ok=True, total=len(batch), done=done,
                    fixed=fixed, skipped=skipped, failed=failed,
                    phase=(
                        f"Uploading {len(group)} to "
                        f"{album or 'library'} (transfers={upload_transfers})…"
                    ),
                )
                uploaded = _upload_many(
                    [item["path"] for item in group],
                    album,
                    transfers=upload_transfers,
                )
                for item in group:
                    path = item["path"]
                    candidate = item["candidate"]
                    name = item["filename"]
                    if path not in uploaded:
                        failed += 1
                        details.append({"filename": name, "result": "upload failed"})
                        path.unlink(missing_ok=True)
                        done += 1
                        continue
                    original = {
                        "id": candidate["id"],
                        "album_id": candidate["album_id"],
                        "filename": name,
                        "album": candidate.get("album", ""),
                        "productUrl": candidate.get("productUrl", ""),
                        "mimeType": candidate.get("mimeType", ""),
                        "creationTime": candidate.get("current", ""),
                        "corrected_date": item["resolved"].isoformat(timespec="seconds"),
                        "source": item["source"],
                    }
                    originals.append(original)
                    _log_original(
                        event="original_superseded",
                        original={
                            "id": original["id"],
                            "filename": original["filename"],
                            "album": original["album"],
                            "album_id": original["album_id"],
                            "productUrl": original["productUrl"],
                            "mimeType": original["mimeType"],
                            "creationTime": original["creationTime"],
                        },
                        corrected={
                            "date": original["corrected_date"],
                            "source": original["source"],
                        },
                    )
                    if candidate["album_id"]:
                        remove_by_album.setdefault(candidate["album_id"], []).append(candidate["id"])
                    fixed += 1
                    details.append({
                        "filename": name, "result": "fixed",
                        "from": candidate["current"],
                        "to": item["resolved"].isoformat(timespec="seconds"),
                        "source": item["source"],
                    })
                    path.unlink(missing_ok=True)
                    done += 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # Corral the originals so the leftover duplicates are one album to
    # select and delete, rather than N items scattered through the
    # timeline. Nothing here can delete them — the API has no
    # mediaItems.delete — but it makes the manual step a couple of clicks.
    # Refresh the access token first: a long apply easily outlives the
    # ~1h cache, and a 401 here used to leave fixed copies uploaded while
    # the wrong-date originals stayed scattered in the timeline.
    corralled = 0
    cleanup_url = ""
    if originals:
        _write_status(mode="apply", finished=False, ok=True, total=len(batch), done=len(batch),
                      fixed=fixed, skipped=skipped, failed=failed,
                      phase="Collecting originals for deletion…")
        token = logic._gphotos_access_token() or token
        try:
            cleanup = logic.find_or_create_album(token, CLEANUP_ALBUM)
            cleanup_url = cleanup.get("url", "")
            added = set(logic.album_add_items(token, cleanup["id"], [item["id"] for item in originals]))
            corralled = len(added)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            _log(f"could not collect originals into {CLEANUP_ALBUM}: {exc}")
            added = set()

        # Only pull an original out of its album once it is safely filed in
        # the cleanup album, or it would become very hard to find again.
        removed = 0
        removed_ids: set[str] = set()
        for album_id, ids in remove_by_album.items():
            safe = [item_id for item_id in ids if item_id in added]
            if not safe:
                continue
            try:
                removed += logic.album_remove_items(token, album_id, safe)
                removed_ids.update(safe)
            except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
                _log(f"could not remove originals from album {album_id}: {exc}")

        for original in originals:
            item_id = original["id"]
            corralled_ok = item_id in added
            _log_original(
                event="original_corralled" if corralled_ok else "original_corral_failed",
                original_id=item_id,
                filename=original["filename"],
                cleanup_album=CLEANUP_ALBUM,
                cleanup_url=cleanup_url if corralled_ok else "",
                removed_from_album=corralled_ok and item_id in removed_ids,
            )
    else:
        removed = 0

    remaining = max(0, len(candidates) - len(batch))
    if corralled:
        leftover = (
            f"{corralled} original{'' if corralled == 1 else 's'} moved to the "
            f"\u201c{CLEANUP_ALBUM}\u201d album — open it and delete them."
        )
    elif fixed:
        leftover = f"{fixed} originals still in your timeline — delete them in Google Photos."
    else:
        leftover = ""
    _write_status(
        mode="apply", finished=True, ok=True, total=len(batch), done=done,
        fixed=fixed, skipped=skipped, failed=failed, removed_from_album=removed,
        corralled=corralled, cleanup_album=CLEANUP_ALBUM, cleanup_url=cleanup_url,
        remaining=remaining, cancelled=_stop_requested, details=details[-50:],
        prepare_workers=prepare_workers, upload_transfers=upload_transfers,
        phase=f"{fixed} fixed, {skipped} skipped, {failed} failed. {leftover}".strip(),
    )
    return 0


def main() -> int:
    global _stop_requested

    def handle_stop(_signum, _frame) -> None:
        global _stop_requested
        _stop_requested = True

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    ensure_private_path(DATEFIX_STATUS_PATH.parent, is_dir=True)
    rotate_log(DATEFIX_LOG_PATH)
    rotate_log(DATEFIX_ORIGINALS_LOG_PATH)

    lock = _acquire_lock()
    if lock is None:
        _write_status(finished=True, ok=False, error="An upload is already running.")
        return 1

    _clear_stale_workdirs()

    try:
        request = read_json(DATEFIX_REQUEST_PATH, {})
        if not isinstance(request, dict) or not request.get("mode"):
            _write_status(finished=True, ok=False, error="No date-fix request to run.")
            return 1
        mode = request["mode"]
        _write_status(mode=mode, finished=False, ok=True, phase="Starting…")
        if mode == "scan":
            return run_scan(request)
        if mode == "apply":
            return run_apply(request)
        _write_status(finished=True, ok=False, error=f"Unknown mode {mode}")
        return 1
    except Exception as exc:  # noqa: BLE001 — must always land in the status file
        _log(f"unhandled error: {exc}")
        _write_status(finished=True, ok=False, error=str(exc))
        return 1
    finally:
        _release_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
