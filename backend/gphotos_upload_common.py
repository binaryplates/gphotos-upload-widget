#!/usr/bin/env python3
"""Shared helpers for the Google Photos upload widget and worker."""
from __future__ import annotations

import fcntl
import json
import os
import time
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

APP_CONFIG_DIR = Path.home() / ".config/gphotos-upload-widget"
CONFIG_PATH = APP_CONFIG_DIR / "config.json"
CONFIG_LOCK_PATH = APP_CONFIG_DIR / "config.lock"
API_CACHE_PATH = APP_CONFIG_DIR / "api-cache.json"
UPLOAD_LOG_PATH = APP_CONFIG_DIR / "upload.log"
UPLOAD_STATUS_PATH = APP_CONFIG_DIR / "upload.status"
UPLOAD_LOCK_PATH = APP_CONFIG_DIR / "upload.lock"
DATEFIX_STATUS_PATH = APP_CONFIG_DIR / "datefix.status"
DATEFIX_REQUEST_PATH = APP_CONFIG_DIR / "datefix.request"
DATEFIX_LOG_PATH = APP_CONFIG_DIR / "datefix.log"
DATEFIX_ORIGINALS_LOG_PATH = APP_CONFIG_DIR / "datefix-originals.jsonl"

PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700
LOG_ROTATE_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
SOURCE_MEDIA_CACHE_TTL = 30.0

MEDIA_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".heic",
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".3gp",
}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp"}
_SOURCE_MEDIA_CACHE: dict[str, tuple[int, float, set[str], set[str]]] = {}


def default_config() -> dict[str, Any]:
    return {
        "autostart": False,
        "visible": False,
        "refresh_seconds": 5,
        "sources": [],
        "google_api_key": "",
        # Cap on how many items one date-fix run will re-upload, so a first
        # run cannot burn the whole 10k/day Photos API quota.
        "datefix_max_items": 500,
        "datefix_prepare_workers": 4,
        "datefix_upload_transfers": 4,
        "datefix_chunk_size": 8,
        "icon_x": 0,
        "icon_y": 0,
        "opacity_percent": 100,
    }


def mask_secret(value: str) -> str:
    value = str(value or "")
    if not value:
        return "Not configured"
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def ensure_private_path(path: Path, *, is_dir: bool = False) -> None:
    try:
        if is_dir:
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, PRIVATE_DIR_MODE)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                os.chmod(path, PRIVATE_FILE_MODE)
            else:
                path.touch()
                os.chmod(path, PRIVATE_FILE_MODE)
    except OSError:
        pass


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write_text(path: Path, content: str, *, mode: int = PRIVATE_FILE_MODE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_private_path(path.parent, is_dir=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def atomic_write_json(path: Path, data: Any, *, mode: int = PRIVATE_FILE_MODE) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=mode)


@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_private_path(path.parent, is_dir=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def update_json_file(
    path: Path,
    transform: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    lock_path: Path | None = None,
    default: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lock_target = lock_path or path.with_suffix(path.suffix + ".lock")
    with file_lock(lock_target):
        current = read_json(path, default or {})
        if not isinstance(current, dict):
            current = dict(default or {})
        updated = transform(dict(current))
        if not isinstance(updated, dict):
            raise TypeError("transform must return a dict")
        atomic_write_json(path, updated)
        return updated


def _canonicalize_path(raw_path: str) -> Path:
    return Path(raw_path).expanduser().resolve(strict=True)


def _album_name(item: Mapping[str, Any], fallback: str) -> str:
    album = str(item.get("album") or fallback).strip()
    if not album:
        raise ValueError("album name is required")
    if any(ch in album for ch in ("/", "\\", "\0")):
        raise ValueError(f"album name contains an unsupported separator: {album!r}")
    return album


def _flag(item: Mapping[str, Any], key: str) -> bool:
    value = item.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "paused", "cancelled", "canceled"}
    return bool(value)


def validate_source_mappings(items: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, str]], list[str]]:
    normalized: list[dict[str, str]] = []
    errors: list[str] = []
    canonical_paths: list[Path] = []

    for index, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            errors.append(f"Source {index}: entry is not a mapping")
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            errors.append(f"Source {index}: path is required")
            continue
        try:
            resolved = _canonicalize_path(raw_path)
        except OSError as exc:
            errors.append(f"Source {index}: {raw_path!r} is not accessible ({exc})")
            continue
        if not resolved.is_dir():
            errors.append(f"Source {index}: {resolved} is not a folder")
            continue
        try:
            fallback = resolved.name or str(resolved)
            album = _album_name(item, fallback)
        except ValueError as exc:
            errors.append(f"Source {index}: {exc}")
            continue
        duplicate = False
        for previous in canonical_paths:
            if resolved == previous:
                errors.append(f"Source {index}: duplicate folder {resolved}")
                duplicate = True
                break
            if resolved in previous.parents:
                errors.append(f"Source {index}: {resolved} is nested inside {previous}")
                duplicate = True
                break
            if previous in resolved.parents:
                errors.append(f"Source {index}: {resolved} contains {previous}; nested sources are not allowed")
                duplicate = True
                break
        if duplicate:
            continue
        normalized.append(
            {
                "path": str(resolved),
                "label": str(item.get("label") or fallback),
                "album": album,
                "paused": _flag(item, "paused"),
                "cancelled": _flag(item, "cancelled"),
            }
        )
        canonical_paths.append(resolved)

    return normalized, errors


def normalize_sources_for_read(items: Any) -> list[dict[str, str]]:
    if not isinstance(items, list):
        return []
    normalized, _errors = validate_source_mappings(items)
    return normalized


def has_runnable_sources(items: Iterable[Mapping[str, Any]]) -> bool:
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if not _flag(item, "paused") and not _flag(item, "cancelled"):
            return True
    return False


def save_config_fields(changes: Mapping[str, Any]) -> dict[str, Any]:
    def transform(current: dict[str, Any]) -> dict[str, Any]:
        next_cfg = default_config()
        next_cfg.update(current)
        next_cfg.update(dict(changes))
        if "sources" in changes:
            sources, errors = validate_source_mappings(next_cfg.get("sources") or [])
            if errors:
                raise ValueError("; ".join(errors))
            next_cfg["sources"] = sources
        return next_cfg

    return update_json_file(CONFIG_PATH, transform, lock_path=CONFIG_LOCK_PATH, default=default_config())


def rotate_log(path: Path, *, backups: int = LOG_BACKUPS, max_bytes: int = LOG_ROTATE_BYTES) -> None:
    try:
        if not path.is_file() or path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    for index in range(backups, 0, -1):
        src = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        dst = path.with_name(f"{path.name}.{index}")
        try:
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
        except OSError:
            pass


def read_worker_status(path: Path) -> dict[str, Any]:
    data = read_json(path, {})
    return data if isinstance(data, dict) else {}


def write_worker_status(path: Path, data: Mapping[str, Any]) -> None:
    ensure_private_path(path.parent, is_dir=True)
    atomic_write_json(path, dict(data))


def status_message(status: Mapping[str, Any] | None) -> str:
    if not isinstance(status, Mapping) or not status:
        return "—"
    for key in ("message", "phase", "error"):
        value = status.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "—"


def count_media_entries(folder: Path) -> tuple[int, int, int]:
    images, videos = list_source_media(folder)
    return len(images), len(videos), len(images) + len(videos)


def merge_progress_counts(limit: int, *counts: Any) -> int:
    bounded = []
    for count in counts:
        try:
            bounded.append(max(0, min(int(count), limit)))
        except (TypeError, ValueError):
            continue
    return max(bounded) if bounded else 0


def list_source_media(folder: Path) -> tuple[set[str], set[str]]:
    """Return recursive image/video paths with a short-lived cache."""
    if not folder.is_dir():
        return set(), set()
    key = str(folder)
    try:
        mtime = folder.stat().st_mtime_ns
    except OSError:
        return set(), set()
    now = time.monotonic()
    cached = _SOURCE_MEDIA_CACHE.get(key)
    if cached and cached[0] == mtime and now - cached[1] < SOURCE_MEDIA_CACHE_TTL:
        return cached[2], cached[3]

    images: set[str] = set()
    videos: set[str] = set()
    try:
        for item in folder.rglob("*"):
            if not item.is_file():
                continue
            suffix = item.suffix.lower()
            if suffix not in MEDIA_EXTS:
                continue
            relative = str(item.relative_to(folder))
            if suffix in VIDEO_EXTS:
                videos.add(relative)
            else:
                images.add(relative)
    except OSError:
        return set(), set()

    _SOURCE_MEDIA_CACHE[key] = (mtime, now, images, videos)
    return images, videos
