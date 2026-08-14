#!/usr/bin/env python3
"""Upload user-selected folders to Google Photos through the configured rclone remote."""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from gphotos_upload_common import (  # noqa: E402
    CONFIG_LOCK_PATH,
    CONFIG_PATH,
    MEDIA_EXTS,
    PRIVATE_FILE_MODE,
    UPLOAD_LOCK_PATH,
    UPLOAD_LOG_PATH,
    UPLOAD_STATUS_PATH,
    atomic_write_json,
    count_media_entries,
    default_config,
    ensure_private_path,
    normalize_sources_for_read,
    read_json,
    rotate_log,
    save_config_fields,
    validate_source_mappings,
    write_worker_status,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config() -> dict:
    cfg = default_config()
    loaded = read_json(CONFIG_PATH, {})
    if isinstance(loaded, dict):
        cfg.update(loaded)
    cfg["sources"] = normalize_sources_for_read(cfg.get("sources"))
    return cfg


def write_terminal_status(*, phase: str, message: str, exit_code: int, error: str = "", active: bool = False, source: dict | None = None, current_relative_path: str = "", current_album: str = "", totals: dict | None = None, completed: dict | None = None, sources: list[dict] | None = None, started_at: str = "", finished_at: str = "") -> None:
    write_worker_status(
        UPLOAD_STATUS_PATH,
        {
            "phase": phase,
            "message": message,
            "active": active,
            "source": source or {},
            "current_relative_path": current_relative_path,
            "current_album": current_album,
            "totals": totals or {"images": 0, "videos": 0, "total": 0},
            "completed": completed or {"images": 0, "videos": 0, "total": 0},
            "sources": sources or [],
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "error": error,
            "timestamp": utc_now(),
        },
    )


def _filter_patterns() -> list[str]:
    patterns: list[str] = []
    for ext in sorted({suffix.lower() for suffix in MEDIA_EXTS}):
        patterns.append(f"**/*{ext}")
        patterns.append(f"*{ext}")
    return patterns


def _rclone_command(source_path: Path, album: str) -> list[str]:
    command = [
        "rclone",
        "copy",
        str(source_path),
        f"gphotos:album/{album}",
        "--transfers",
        "1",
        "--checkers",
        "1",
        "--retries",
        "3",
        "--low-level-retries",
        "10",
        "--stats",
        "5s",
        "--stats-one-line",
        "--log-level",
        "INFO",
        "--log-file",
        str(UPLOAD_LOG_PATH),
    ]
    for pattern in _filter_patterns():
        command.extend(["--include", pattern])
    command.extend(["--exclude", "*"])
    return command


def _update_source_summary(
    summary: dict,
    *,
    message: str | None = None,
    current_relative_path: str = "",
    completed_images: int | None = None,
    completed_videos: int | None = None,
    completed_total: int | None = None,
    error: str = "",
    exit_code: int | None = None,
    finished: bool = False,
) -> dict:
    if message is not None:
        summary["message"] = message
    if current_relative_path:
        summary["current_relative_path"] = current_relative_path
    if completed_images is not None or completed_videos is not None or completed_total is not None:
        summary["completed"] = {
            "images": int(completed_images or 0),
            "videos": int(completed_videos or 0),
            "total": int(completed_total or 0),
        }
    if error:
        summary["error"] = error
    if exit_code is not None:
        summary["exit_code"] = exit_code
    if finished:
        summary["finished_at"] = utc_now()
    return summary


def _initial_source_summary(source: dict, images: int, videos: int) -> dict:
    total = images + videos
    cancelled = bool(source.get("cancelled"))
    paused = bool(source.get("paused"))
    return {
        "path": source["path"],
        "label": source["label"],
        "album": source["album"],
        "paused": paused,
        "cancelled": cancelled,
        "state": "cancelled" if cancelled else "paused" if paused else "ready",
        "images": images,
        "videos": videos,
        "total": total,
        "completed": {"images": 0, "videos": 0, "total": 0},
        "current_relative_path": "",
        "message": "Queued",
        "error": "",
        "exit_code": None,
        "started_at": utc_now(),
        "finished_at": "",
    }


def _should_skip_source(source: dict) -> bool:
    return bool(source.get("paused") or source.get("cancelled"))


def _read_rclone_config_path() -> Path:
    fallback = Path.home() / ".config/rclone/rclone.conf"
    try:
        proc = subprocess.run(["rclone", "config", "file"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return fallback
    text = f"{proc.stdout}\n{proc.stderr}"
    for token in text.split():
        if token.endswith("rclone.conf") and "/" in token:
            return Path(token.strip())
    return fallback


def _acquire_lock() -> bool:
    ensure_private_path(UPLOAD_LOCK_PATH.parent, is_dir=True)
    handle = UPLOAD_LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    os.set_inheritable(handle.fileno(), False)
    globals()["_LOCK_HANDLE"] = handle
    return True


def _release_lock() -> None:
    handle = globals().pop("_LOCK_HANDLE", None)
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass


def _run_rclone(command: list[str], stop_requested) -> int:
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(command)
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc
    while proc.poll() is None:
        if stop_requested():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            return 130
        time.sleep(0.2)
    return int(proc.returncode or 0)


def main() -> int:
    ensure_private_path(UPLOAD_STATUS_PATH.parent, is_dir=True)
    ensure_private_path(UPLOAD_LOG_PATH.parent, is_dir=True)
    ensure_private_path(UPLOAD_LOG_PATH)
    rotate_log(UPLOAD_LOG_PATH)
    if not _acquire_lock():
        write_terminal_status(
            phase="already_running",
            message="Upload worker already running.",
            exit_code=0,
            active=True,
            finished_at=utc_now(),
        )
        return 0

    stop_requested = False

    def handle_stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous_term = signal.signal(signal.SIGTERM, handle_stop)
    previous_int = signal.signal(signal.SIGINT, handle_stop)

    try:
        cfg = load_config()
        sources, errors = validate_source_mappings(cfg.get("sources") or [])
        if errors or not sources:
            message = "No valid source folders configured." if not sources else "; ".join(errors)
            write_terminal_status(
                phase="invalid_configuration",
                message=message,
                exit_code=0,
                error=message,
                active=False,
                sources=[],
                finished_at=utc_now(),
            )
            return 0

        runnable_sources = [source for source in sources if not _should_skip_source(source)]
        if not runnable_sources:
            summaries = []
            for source in sources:
                source_path = Path(source["path"])
                images, videos, _total = count_media_entries(source_path)
                summary = _initial_source_summary(source, images, videos)
                summary = _update_source_summary(
                    summary,
                    message="Source paused." if source.get("paused") and not source.get("cancelled") else "Source cancelled.",
                    exit_code=0,
                    finished=True,
                )
                summaries.append(summary)
            write_terminal_status(
                phase="paused",
                message="All configured source folders are paused or cancelled.",
                exit_code=0,
                active=False,
                totals={"images": 0, "videos": 0, "total": 0},
                completed={"images": 0, "videos": 0, "total": 0},
                sources=summaries,
                finished_at=utc_now(),
            )
            return 0

        summaries: list[dict] = []
        totals = {"images": 0, "videos": 0, "total": 0}
        completed = {"images": 0, "videos": 0, "total": 0}
        started_at = utc_now()
        write_terminal_status(
            phase="starting",
            message="Preparing upload worker.",
            exit_code=0,
            active=True,
            totals=totals,
            completed=completed,
            sources=summaries,
            started_at=started_at,
        )

        for source in sources:
            if stop_requested:
                break
            source_path = Path(source["path"])
            images, videos, total = count_media_entries(source_path)
            summary = _initial_source_summary(source, images, videos)
            summaries.append(summary)
            if _should_skip_source(source):
                summary = _update_source_summary(
                    summary,
                    message="Source paused." if source.get("paused") and not source.get("cancelled") else "Source cancelled.",
                    exit_code=0,
                    finished=True,
                )
                summaries[-1] = summary
                write_terminal_status(
                    phase="paused" if source.get("paused") and not source.get("cancelled") else "cancelled",
                    message=summary["message"],
                    exit_code=0,
                    active=True,
                    source=summary,
                    current_relative_path=source_path.name,
                    current_album=source["album"],
                    totals=totals,
                    completed=completed,
                    sources=summaries,
                    started_at=started_at,
                )
                continue
            totals["images"] += images
            totals["videos"] += videos
            totals["total"] += total
            write_terminal_status(
                phase="uploading",
                message=f"Uploading {source_path} to {source['album']}.",
                exit_code=0,
                active=True,
                source=summary,
                current_relative_path=source_path.name,
                current_album=source["album"],
                totals=totals,
                completed=completed,
                sources=summaries,
                started_at=started_at,
            )
            command = _rclone_command(source_path, source["album"])
            try:
                code = _run_rclone(command, lambda: stop_requested)
            except RuntimeError as exc:
                error = str(exc)
                summary = _update_source_summary(summary, message="Upload failed.", error=error, exit_code=1, finished=True)
                write_terminal_status(
                    phase="failed",
                    message=error,
                    exit_code=1,
                    error=error,
                    active=False,
                    source=summary,
                    current_relative_path=source_path.name,
                    current_album=source["album"],
                    totals=totals,
                    completed=completed,
                    sources=summaries,
                    started_at=started_at,
                    finished_at=utc_now(),
                )
                return 1

            if code == 0:
                completed["images"] += images
                completed["videos"] += videos
                completed["total"] += total
                summary = _update_source_summary(
                    summary,
                    message="Upload complete.",
                    completed_images=images,
                    completed_videos=videos,
                    completed_total=total,
                    exit_code=0,
                    finished=True,
                )
                summaries[-1] = summary
                write_terminal_status(
                    phase="uploading",
                    message=f"Uploaded {source_path} to {source['album']}.",
                    exit_code=0,
                    active=True,
                    source=summary,
                    current_relative_path=source_path.name,
                    current_album=source["album"],
                    totals=totals,
                    completed=completed,
                    sources=summaries,
                    started_at=started_at,
                )
                continue

            if stop_requested or code in {130, -signal.SIGTERM}:
                summary = _update_source_summary(
                    summary,
                    message="Upload cancelled.",
                    completed_images=0,
                    completed_videos=0,
                    completed_total=0,
                    error="Termination requested",
                    exit_code=130,
                    finished=True,
                )
                summaries[-1] = summary
                write_terminal_status(
                    phase="cancelled",
                    message="Upload worker cancelled.",
                    exit_code=130,
                    error="Termination requested",
                    active=False,
                    source=summary,
                    current_relative_path=source_path.name,
                    current_album=source["album"],
                    totals=totals,
                    completed=completed,
                    sources=summaries,
                    started_at=started_at,
                    finished_at=utc_now(),
                )
                return 130

            error = f"rclone exited with {code}"
            summary = _update_source_summary(
                summary,
                message="Upload failed.",
                completed_images=0,
                completed_videos=0,
                completed_total=0,
                error=error,
                exit_code=code,
                finished=True,
            )
            summaries[-1] = summary
            write_terminal_status(
                phase="failed",
                message=error,
                exit_code=code,
                error=error,
                active=False,
                source=summary,
                current_relative_path=source_path.name,
                current_album=source["album"],
                totals=totals,
                completed=completed,
                sources=summaries,
                started_at=started_at,
                finished_at=utc_now(),
            )
            return code

        phase = "completed" if not stop_requested else "cancelled"
        message = "All active source folders uploaded." if not stop_requested else "Upload worker cancelled."
        write_terminal_status(
            phase=phase,
            message=message,
            exit_code=0 if not stop_requested else 130,
            active=False,
            totals=totals,
            completed=completed,
            sources=summaries,
            started_at=started_at,
            finished_at=utc_now(),
        )
        return 0 if not stop_requested else 130
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        _release_lock()


if __name__ == "__main__":
    sys.exit(main())
