"""Data/action layer backing net.blazorplate.GPhotosUpload.

Folded from the former gphotos_cli.py JSON CLI wrapper — same functions,
same behavior, now called by dbus_service.py's method handlers instead of
an argparse main().
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from gphotos_upload_common import (
    API_CACHE_PATH,
    PRIVATE_FILE_MODE,
    UPLOAD_LOG_PATH,
    UPLOAD_STATUS_PATH,
    atomic_write_json,
    default_config,
    list_source_media,
    mask_secret,
    merge_progress_counts,
    normalize_sources_for_read,
    read_json,
    read_worker_status,
    save_config_fields,
    status_message,
    CONFIG_PATH,
)

SERVICE = "rclone-gphotos.service"
DAILY_QUOTA = 10_000
JORDAN = ZoneInfo("Asia/Amman")
QUOTA_RESUME_HOUR, QUOTA_RESUME_MINUTE = 10, 45

_BYTE_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
_STAT_RE = re.compile(
    r"^(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+NOTICE:\s+"
    r"([\d.]+)\s+(B|KiB|MiB|GiB)\s+/\s+([\d.]+)\s+(B|KiB|MiB|GiB),\s+"
    r"(?:(\d+)%|-),\s+([\d.]+)\s+(B|KiB|MiB|GiB)/s,\s+ETA\s+(\S+)"
    r"(?:\s+\(xfr#(\d+)/(\d+)\))?",
    re.M,
)
_START_RE = re.compile(
    r"^(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+INFO\s+:\s+Starting transaction limiter",
    re.M,
)
_COPIED_RE = re.compile(
    r"^(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+INFO\s+:\s+(.+?):\s+Copied \(new\)",
    re.M,
)
_COMMIT_RE = re.compile(
    r"^(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+INFO\s+:\s+"
    r"Google Photos path .*: Committing uploads - please wait",
    re.M,
)


# ---------------------------------------------------------------- config ---

def load_config() -> dict:
    cfg = default_config()
    loaded = read_json(CONFIG_PATH, {})
    if isinstance(loaded, dict):
        cfg.update(loaded)
    cfg["sources"] = normalize_sources_for_read(cfg.get("sources"))
    return cfg


# --------------------------------------------------------------- service ---

def service_active() -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return proc.stdout.strip() == "active"
    except (OSError, subprocess.TimeoutExpired):
        return False


def set_service(on: bool) -> tuple[bool, str]:
    cmd = "start" if on else "stop"
    try:
        proc = subprocess.run(
            ["systemctl", "--user", cmd, SERVICE],
            capture_output=True, text=True, timeout=60 if not on else 20, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"systemd {cmd} timed out"
    except OSError as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or f"{cmd} failed").strip()
    return True, ""


# ----------------------------------------------------------- log parsing ---

def _to_bps(value: str, unit: str) -> float:
    return float(value) * _BYTE_UNITS.get(unit, 1)


def fmt_rate(bps: float) -> str:
    if bps >= 1024**2:
        return f"{bps / 1024**2:.2f} MiB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f} KiB/s"
    if bps > 0:
        return f"{bps:.0f} B/s"
    return "0 B/s"


def rate_upload_speed(bps: float, *, stale: bool, starting: bool = False) -> tuple[str, str, str, float]:
    if stale:
        return ("STALLED", "rate-stalled", "No fresh rclone stats — upload may be waiting on Google.", 0.05)
    if starting or bps <= 0:
        return ("WAIT", "rate-fair", "rclone is running this batch — waiting on Google Photos.", 0.08)
    kib = bps / 1024.0
    if kib < 8:
        return "BAD", "rate-bad", "Far below typical Photos API pace. Likely throttling or a stall.", min(0.12, max(0.04, kib / 40))
    if kib < 20:
        return "POOR", "rate-poor", "Slow but normal for Google Photos (API-limited, not your Wi-Fi).", 0.28
    if kib < 80:
        return "FAIR", "rate-fair", "Typical Photos API throughput with 1 transfer / 1 request per second.", 0.48
    if kib < 300:
        return "GOOD", "rate-good", "Strong for Photos API — bursts like this usually do not last.", 0.72
    return "EXCELLENT", "rate-excellent", "Unusually fast for Photos API. Enjoy it while it lasts.", 0.95


def parse_upload_metrics(log: Path, worker_started_at: str | None = None) -> dict:
    empty = {
        "text": "—", "instant_bps": 0.0, "avg_bps": 0.0, "files_per_min": 0.0,
        "eta": "", "xfr": "", "age_secs": None, "label": "—", "css": "meta",
        "hint": "Speed appears here once rclone starts transferring.", "bar": 0.0, "fresh": False,
    }
    if not log.is_file():
        return empty
    text = log.read_bytes()[-1_000_000:].decode("utf-8", "replace")
    session_start = None
    if worker_started_at:
        try:
            started = datetime.fromisoformat(worker_started_at)
            if started.tzinfo is not None:
                started = started.astimezone().replace(tzinfo=None)
            session_start = started
        except (TypeError, ValueError, OverflowError):
            session_start = None
    marker_start = None
    for m in _START_RE.finditer(text):
        try:
            marker_start = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
        except ValueError:
            continue
    if session_start is None:
        session_start = marker_start
    samples = []
    for m in _STAT_RE.finditer(text):
        try:
            ts = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
        except ValueError:
            continue
        if session_start and ts < session_start:
            continue
        samples.append({
            "ts": ts, "bps": _to_bps(m.group(7), m.group(8)), "eta": m.group(9),
            "xfr_done": int(m.group(10) or 0), "xfr_total": int(m.group(11) or 0),
        })
    now = datetime.now()
    copy_times: list[datetime] = []
    for m in _COPIED_RE.finditer(text):
        try:
            ts = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
        except ValueError:
            continue
        if session_start and ts < session_start:
            continue
        copy_times.append(ts)
    span = 600.0
    if copy_times:
        span = max(30.0, min(600.0, (now - min(copy_times)).total_seconds() or 30.0))
    files_per_min = len(copy_times) / (span / 60.0) if copy_times else 0.0
    commit_times = []
    for m in _COMMIT_RE.finditer(text):
        try:
            commit_times.append(datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S"))
        except ValueError:
            continue
    latest_commit = commit_times[-1] if commit_times else None
    latest_copy = copy_times[-1] if copy_times else None
    latest_sample = samples[-1]["ts"] if samples else None
    if latest_commit and (latest_copy is None or latest_commit >= latest_copy) and (
        latest_sample is None or latest_commit >= latest_sample
    ):
        return {**empty, "hint": "Google Photos is committing uploads — transfer speed is temporarily unavailable.", "files_per_min": files_per_min}
    if not samples:
        if not copy_times:
            return empty
        est_bps = files_per_min * 350_000 / 60.0
        label, css, hint, bar = rate_upload_speed(est_bps, stale=False, starting=files_per_min < 0.2)
        hint = "From copy rate — rclone byte stats appear every few seconds."
        return {
            "text": f"{files_per_min:.1f} files/min", "instant_bps": est_bps, "avg_bps": est_bps,
            "files_per_min": files_per_min, "eta": "", "xfr": "", "age_secs": (now - copy_times[-1]).total_seconds(),
            "label": label, "css": css, "hint": hint, "bar": bar, "fresh": files_per_min > 0,
            "starting": files_per_min < 0.2,
        }
    last = samples[-1]
    age = (now - last["ts"]).total_seconds()
    recent = age <= 180 or bool(copy_times)
    starting = recent and last["bps"] <= 0 and not copy_times
    stale = not recent
    window = [s["bps"] for s in samples[-12:] if s["bps"] > 0]
    avg_bps = (sum(window) / len(window)) if window else 0.0
    instant = last["bps"] if age <= 180 else 0.0
    if instant <= 0 and copy_times:
        instant = files_per_min * 350_000 / 60.0
        avg_bps = max(avg_bps, instant)
        recent = True
        stale = False
    label, css, hint, bar = rate_upload_speed(instant, stale=stale, starting=starting)
    xfr = ""
    if recent and last["xfr_total"]:
        xfr = f"{last['xfr_done']}/{last['xfr_total']} this batch"
    bits = [fmt_rate(instant) if recent and instant > 0 else "—"]
    if last["eta"] and age <= 180 and last["eta"] not in {"0s", "-"}:
        bits.append(f"ETA {last['eta']}")
    return {
        "text": " · ".join(bits), "instant_bps": instant, "avg_bps": avg_bps, "files_per_min": files_per_min,
        "eta": last["eta"] if age <= 180 else "", "xfr": xfr, "age_secs": age, "label": label, "css": css,
        "hint": hint, "bar": bar, "fresh": recent and instant > 0, "starting": starting,
    }


def quota_resume_at(now: datetime | None = None) -> datetime:
    now = now or datetime.now(JORDAN)
    target = now.replace(hour=QUOTA_RESUME_HOUR, minute=QUOTA_RESUME_MINUTE, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target


def last_quota_reset_at(now: datetime | None = None) -> datetime:
    return quota_resume_at(now) - timedelta(days=1)


def seconds_until_quota_resume() -> int:
    now = datetime.now(JORDAN)
    return max(0, int((quota_resume_at(now) - now).total_seconds()))


def quota_exhausted(log: Path) -> bool:
    if not log.is_file():
        return False
    reset = last_quota_reset_at().replace(tzinfo=None)
    tail = log.read_bytes()[-200_000:].decode("utf-8", "replace")
    for line in tail.splitlines():
        if "All requests per day" not in line or "RESOURCE_EXHAUSTED" not in line:
            continue
        try:
            ts = datetime.strptime(line[:19], "%Y/%m/%d %H:%M:%S")
        except ValueError:
            continue
        if ts >= reset:
            return True
    return False


def log_progress(log: Path, media_names: dict[str, set[str]]) -> dict:
    reset = last_quota_reset_at().replace(tzinfo=None)
    copied_by_kind = {"images": set(), "videos": set()}
    today: set[tuple[str, str]] = set()
    first_copy = None
    last_start = None
    if log.is_file():
        data = log.read_bytes()
        if len(data) > 2_000_000:
            data = data[-2_000_000:]
        text = data.decode("utf-8", "replace")
        for m in _START_RE.finditer(text):
            try:
                ts = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
            except ValueError:
                continue
            if ts >= reset:
                last_start = ts
        for m in _COPIED_RE.finditer(text):
            try:
                ts = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
            except ValueError:
                continue
            raw_path = m.group(2).strip().replace("\\", "/")
            parts = {part.lower() for part in Path(raw_path).parts}
            matches = [kind for kind, names in media_names.items() if raw_path in names]
            if "images" in parts and raw_path in media_names.get("images", set()):
                kind = "images"
            elif "videos" in parts and raw_path in media_names.get("videos", set()):
                kind = "videos"
            elif len(matches) == 1:
                kind = matches[0]
            else:
                continue
            copied_by_kind[kind].add(raw_path)
            if ts >= reset:
                today.add((kind, raw_path))
                if first_copy is None or ts < first_copy:
                    first_copy = ts
                continue
    copies_today = len(today)
    write_calls = copies_today * 2
    dest_check = 0
    now = datetime.now()
    if last_start:
        if first_copy and first_copy >= last_start:
            dest_check = int((first_copy - last_start).total_seconds())
        elif first_copy is None:
            dest_check = int((now - last_start).total_seconds())
    quota_used = min(DAILY_QUOTA, max(0, write_calls + dest_check))
    return {
        "copied_by_kind": copied_by_kind,
        "copied_names": copied_by_kind["images"] | copied_by_kind["videos"],
        "copies_today": copies_today,
        "quota_used": quota_used,
        "reset_clock": last_quota_reset_at().strftime("%-I:%M %p"),
    }


def fmt_duration(secs: int) -> str:
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def progress_percent(done: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * done / total))


# --------------------------------------------------------------- Google ---

def _load_api_cache() -> dict:
    if API_CACHE_PATH.is_file():
        try:
            loaded = json.loads(API_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _save_api_cache(data: dict) -> None:
    atomic_write_json(API_CACHE_PATH, data, mode=PRIVATE_FILE_MODE)


def fetch_drive_storage(*, force: bool = False) -> dict:
    cache = _load_api_cache()
    now = time.time()
    st = dict(cache.get("drive") or {})
    if not force and now - float(st.get("ts") or 0) < 180 and st.get("total"):
        st["ok"] = True
        return st
    try:
        proc = subprocess.run(
            ["rclone", "about", "gdrive:", "--json", "--retries", "1"],
            capture_output=True, text=True, timeout=6, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            drive_files = int(data.get("used") or 0)
            other = int(data.get("other") or 0)
            trashed = int(data.get("trashed") or 0)
            st = {
                "ts": now, "ok": True,
                "total": int(data.get("total") or 0),
                # rclone's "used" is Drive-owned files only; Google's own account
                # quota (what drive.google.com shows) also counts "other"
                # (Gmail/Photos) and trashed items — match that total here.
                "used": drive_files + other + trashed,
                "drive_files_used": drive_files,
                "free": int(data.get("free") or 0), "other": other,
                "trashed": trashed,
            }
            cache["drive"] = st
            _save_api_cache(cache)
            return st
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        if st.get("total"):
            st["ok"] = True
            return st
    st["ok"] = False
    return st


def _gphotos_access_token() -> str:
    proc = subprocess.run(["rclone", "config", "dump"], capture_output=True, text=True, timeout=10, check=False)
    dump = json.loads(proc.stdout or "{}")
    raw = dump.get("gphotos", {}).get("token") or {}
    token = json.loads(raw) if isinstance(raw, str) else raw
    return str(token.get("access_token") or "")


def google_credentials() -> dict[str, str]:
    try:
        proc = subprocess.run(["rclone", "config", "dump"], capture_output=True, text=True, timeout=10, check=False)
        dump = json.loads(proc.stdout or "{}")
        remote = dump.get("gphotos") if isinstance(dump, dict) else {}
        remote = remote if isinstance(remote, dict) else {}
        raw_token = remote.get("token") or {}
        token = json.loads(raw_token) if isinstance(raw_token, str) else raw_token
        token = token if isinstance(token, dict) else {}
        return {
            "client_id": str(remote.get("client_id") or ""),
            "client_secret": str(remote.get("client_secret") or ""),
            "access_token": str(token.get("access_token") or ""),
            "refresh_token": str(token.get("refresh_token") or ""),
            "error": "",
        }
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as exc:
        return {"client_id": "", "client_secret": "", "access_token": "", "refresh_token": "", "error": str(exc)}


def _rclone_config_path() -> Path:
    fallback = Path.home() / ".config/rclone/rclone.conf"
    try:
        proc = subprocess.run(["rclone", "config", "file"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return fallback
    text = "\n".join(part.strip() for part in (proc.stdout or "", proc.stderr or "") if part.strip())
    match = re.search(r"(/[^\s]+rclone\.conf)", text)
    if match:
        return Path(match.group(1))
    return fallback


def _update_rclone_remote_credentials(client_id: str | None = None, client_secret: str | None = None) -> None:
    import configparser
    import os

    config_path = _rclone_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    if config_path.is_file():
        parser.read(config_path, encoding="utf-8")
    if not parser.has_section("gphotos"):
        parser.add_section("gphotos")
    if client_id is not None:
        parser.set("gphotos", "client_id", client_id)
    if client_secret is not None:
        parser.set("gphotos", "client_secret", client_secret)
    temp = config_path.with_suffix(".tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            parser.write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        temp.replace(config_path)
        os.chmod(config_path, 0o600)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def save_google_credentials(client_id: str, client_secret: str, api_key: str) -> tuple[bool, str]:
    api_key_value = api_key.strip()
    oauth_changed = bool(client_id.strip() or client_secret.strip())
    if not oauth_changed and api_key_value:
        try:
            save_config_fields({"google_api_key": api_key_value})
        except ValueError as exc:
            return False, str(exc)
        return True, "API key saved; OAuth client values were unchanged."
    try:
        _update_rclone_remote_credentials(client_id=client_id.strip() or None, client_secret=client_secret.strip() or None)
    except OSError as exc:
        return False, str(exc)
    if api_key_value:
        try:
            save_config_fields({"google_api_key": api_key_value})
        except ValueError as exc:
            return False, str(exc)
    return True, "Google OAuth settings saved."


def test_google_credentials() -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["rclone", "lsd", "gphotos:", "--max-depth", "1", "--checkers", "1"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, "Google OAuth credentials are valid. API key is stored but not validated here."
    return False, (proc.stderr or proc.stdout or "Google OAuth test failed").strip()


def fetch_album_counts(*, skip: bool, force: bool = False, live: bool = False, album_titles: list[str] | None = None) -> dict:
    wanted = set(album_titles or [])
    if not wanted:
        return {"fetched": True, "ok": True, "counts": {}, "urls": {}}
    cache = _load_api_cache()
    now = time.time()
    al = dict(cache.get("albums") or {})
    if force:
        min_age = 45
    elif live:
        min_age = 90
    else:
        min_age = 20 * 60
    cached_counts = al.get("counts") if isinstance(al.get("counts"), dict) else {}
    if not force and now - float(al.get("ts") or 0) < min_age and al.get("fetched") and wanted <= cached_counts.keys():
        return al
    if skip:
        return al
    try:
        token = _gphotos_access_token()
        if not token:
            return al
        counts: dict[str, int] = {}
        urls: dict[str, str] = {}
        page_token = ""
        for _ in range(20):
            query = {"pageSize": "50"}
            if page_token:
                query["pageToken"] = page_token
            req = urllib.request.Request(
                "https://photoslibrary.googleapis.com/v1/albums?" + urlencode(query),
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            for album in body.get("albums") or []:
                title = str(album.get("title") or "")
                counts[title] = int(album.get("mediaItemsCount") or 0)
                if album.get("productUrl"):
                    urls[title] = str(album["productUrl"])
            if wanted <= counts.keys():
                break
            page_token = str(body.get("nextPageToken") or "")
            if not page_token:
                break
        al = {
            "ts": now, "fetched": True, "ok": True,
            "counts": {title: counts.get(title) for title in wanted},
            "urls": {title: urls.get(title, "") for title in wanted},
        }
        cache["albums"] = al
        _save_api_cache(cache)
        return al
    except (OSError, subprocess.TimeoutExpired, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, KeyError):
        al["ok"] = False
        return al


# ------------------------------------------------------------- snapshot ---

def gather_snapshot(sources: list[dict[str, str]], *, force: bool = False, reconcile_remote: bool = False) -> dict:
    worker_status = read_worker_status(UPLOAD_STATUS_PATH)
    running_path = str((worker_status.get("source") or {}).get("path") or "")
    active_sources = [
        item for item in sources
        if not item.get("cancelled") and (not item.get("paused") or str(item.get("path") or "") == running_path)
    ]
    tracked_sources = [item for item in sources if not item.get("cancelled")]
    source_paths = [Path(item["path"]) for item in tracked_sources]
    source_images: set[str] = set()
    source_videos: set[str] = set()
    missing = [path for path in source_paths if not path.is_dir()]
    for path in source_paths:
        images, videos = list_source_media(path)
        source_images.update(images)
        source_videos.update(videos)
    img_total, vid_total = len(source_images), len(source_videos)
    log = UPLOAD_LOG_PATH
    progress = log_progress(log, {"images": source_images, "videos": source_videos})
    copied_by_kind = progress["copied_by_kind"]
    completed = worker_status.get("completed") if isinstance(worker_status.get("completed"), dict) else {}
    progress_counts = worker_status.get("progress") if isinstance(worker_status.get("progress"), dict) else {}
    img_done = merge_progress_counts(img_total, len(copied_by_kind["images"] & source_images), completed.get("images"), progress_counts.get("images"))
    vid_done = merge_progress_counts(vid_total, len(copied_by_kind["videos"] & source_videos), completed.get("videos"), progress_counts.get("videos"))
    total = img_total + vid_total
    done = img_done + vid_done
    status = status_message(worker_status)
    active = service_active()
    waiting = bool("waiting_for_quota" in status or "waiting_until" in status or "waiting_for_quota" in str(worker_status.get("phase") or ""))
    exhausted = quota_exhausted(log)
    resume_secs = seconds_until_quota_resume() if (waiting or exhausted) else 0
    resume_clock = quota_resume_at().strftime("%-I:%M %p")
    worker_phase = str(worker_status.get("phase") or "")
    worker_error = str(worker_status.get("error") or "")
    worker_finished = bool(worker_status.get("finished_at"))
    worker_terminal = worker_finished or worker_phase in {"completed", "failed", "cancelled", "terminated", "invalid_configuration"}
    if not sources:
        phase = "Select a source folder to begin"
    elif not active_sources:
        phase = "All sources are paused or cancelled"
    elif missing:
        phase = f"Missing source: {missing[0]}"
    elif waiting or (exhausted and active):
        phase = f"Paused until {resume_clock} Jordan · {fmt_duration(resume_secs)} left"
    elif worker_terminal and not active:
        phase = status if status != "—" else "Worker finished"
        if worker_error and worker_error not in phase:
            phase = f"{phase} · {worker_error}"
    elif active:
        current_path = str(worker_status.get("current_relative_path") or worker_status.get("current_path") or "")
        current_album = str(worker_status.get("current_album") or "")
        if done >= total > 0 and worker_phase != "uploading":
            phase = "Finalizing Google Photos upload"
        elif current_path and current_album:
            phase = f"Uploading {current_path} → {current_album}"
        elif current_path:
            phase = f"Uploading {current_path}"
        else:
            phase = "Uploading now"
    elif done >= total > 0:
        phase = "All files are in Photos"
    else:
        phase = "Upload is off"
    drive = fetch_drive_storage(force=False)
    album_titles = [item["album"] for item in active_sources]
    albums = fetch_album_counts(
        skip=exhausted or waiting or not active_sources, force=force,
        live=bool(active and not waiting and not exhausted), album_titles=album_titles,
    )
    done = img_done + vid_done
    pct = progress_percent(done, total)
    remaining = max(0, total - done)
    if not active and done >= total > 0:
        phase = "All files are in Photos"
    return {
        "ok": True, "active": active, "phase": phase, "sources": sources,
        "reconciled_remote": reconcile_remote and bool(albums.get("ok")),
        "img_done": img_done, "img_total": img_total, "vid_done": vid_done, "vid_total": vid_total,
        "remote_counts": {str(a): int(c) for a, c in (albums.get("counts") or {}).items() if c is not None},
        "albums_ok": bool(albums.get("ok")),
        "done": done, "total": total, "pct": pct, "remaining": remaining,
        "status": status, "worker_status": worker_status, "worker_terminal": worker_terminal,
        "speed": parse_upload_metrics(log, str(worker_status.get("started_at") or "")),
        "quota_limit": DAILY_QUOTA, "quota_used": progress["quota_used"],
        "copies_today": progress["copies_today"], "quota_reset_clock": progress["reset_clock"],
        "quota_exhausted": exhausted, "waiting": waiting, "resume_secs": resume_secs, "resume_clock": resume_clock,
        "drive": drive, "timestamp": datetime.now().strftime("%H:%M:%S"),
    }


# -------------------------------------------------------------- sources ---

def _mutate_source(path: str, field: str, value) -> dict:
    cfg = load_config()
    sources = cfg.get("sources") or []
    found = False
    for item in sources:
        if item.get("path") == path:
            item[field] = value
            found = True
    if not found:
        return {"ok": False, "error": f"no source configured for {path}"}
    try:
        updated = save_config_fields({"sources": sources})
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "sources": updated.get("sources", [])}


# ------------------------------------------------------ D-Bus dispatch ---
# One function per D-Bus method, each returning a plain dict that
# dbus_service.py will json.dumps() before replying. Mirrors the former
# gphotos_cli.py subcommands 1:1.

def status(force: bool) -> dict:
    cfg = load_config()
    return gather_snapshot(cfg.get("sources", []), force=force, reconcile_remote=False)


def service_start() -> dict:
    ok, error = set_service(True)
    return {"ok": ok, "error": error, "active": service_active()}


def service_stop() -> dict:
    ok, error = set_service(False)
    return {"ok": ok, "error": error, "active": service_active()}


def service_status() -> dict:
    return {"ok": True, "active": service_active()}


def sources_list() -> dict:
    return {"ok": True, "sources": load_config().get("sources", [])}


def sources_add(path: str, album: str) -> dict:
    cfg = load_config()
    sources = cfg.get("sources") or []
    sources.append({"path": path, "album": album or "", "paused": False, "cancelled": False})
    try:
        updated = save_config_fields({"sources": sources})
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "sources": updated.get("sources", [])}


def sources_remove(path: str) -> dict:
    cfg = load_config()
    sources = [item for item in (cfg.get("sources") or []) if item.get("path") != path]
    try:
        updated = save_config_fields({"sources": sources})
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "sources": updated.get("sources", [])}


def sources_pause(path: str) -> dict:
    return _mutate_source(path, "paused", True)


def sources_resume(path: str) -> dict:
    return _mutate_source(path, "paused", False)


def sources_cancel(path: str) -> dict:
    return _mutate_source(path, "cancelled", True)


def credentials_get() -> dict:
    creds = google_credentials()
    cfg = load_config()
    return {
        "ok": not creds.get("error"),
        "client_id": mask_secret(creds.get("client_id", "")),
        "client_secret": mask_secret(creds.get("client_secret", "")),
        "access_token": mask_secret(creds.get("access_token", "")),
        "api_key": mask_secret(cfg.get("google_api_key", "")),
        "error": creds.get("error", ""),
    }


def credentials_set(client_id: str, client_secret: str, api_key: str) -> dict:
    ok, message = save_google_credentials(client_id or "", client_secret or "", api_key or "")
    return {"ok": ok, "message": message}


def credentials_test() -> dict:
    ok, message = test_google_credentials()
    return {"ok": ok, "message": message}


def storage_quota(force: bool) -> dict:
    return fetch_drive_storage(force=force)


def reconcile() -> dict:
    cfg = load_config()
    return gather_snapshot(cfg.get("sources", []), force=True, reconcile_remote=True)
