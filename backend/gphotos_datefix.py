"""Pure helpers for working out an item's real creation date.

Deliberately free of I/O so the whole decision layer is unit-testable: the
worker does the downloading, exiftool calls and uploading, and asks these
functions what date a file should get and whether it needs fixing at all.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# A parsed date outside this window is a false positive from some unrelated
# digit run in the filename, never a real capture date.
MIN_YEAR = 1990
FUTURE_SLACK = timedelta(days=1)

# Google stores creationTime in UTC while filename dates are local wall
# clock, so a same-day item can legitimately differ by most of a day. Only
# flag items that are off by more than a full day — the failure this feature
# exists for (Google falling back to upload time) is off by months or years.
MISDATE_TOLERANCE = timedelta(hours=24)

# A filename that carries only a date, no clock time, is worth a day at
# best. WhatsApp names are the common case: VID-20180924-WA0000 says the
# 24th, while Google may hold a precise timestamp a day or two either side
# that is almost certainly the better value. Only override a date-only
# guess when Google is off by more than this.
DAY_PRECISION_TOLERANCE = timedelta(days=7)

SECOND_PRECISION = "second"
DAY_PRECISION = "day"

VIDEO_MIME_PREFIX = "video/"

# Ordered most-specific first: a filename carrying a full timestamp must not
# be matched by the bare-date patterns further down.
# A trailing run of up to 3 digits is Pixel's millisecond suffix
# (PXL_20230415_123456789), not part of the time.
_SUBSEC = r"(?:\d{1,3})?(?!\d)"

# (pattern, how precise a match from it actually is)
_FILENAME_PATTERNS = (
    # 20230415_123456, IMG_20230415_123456, PXL_20230415_123456789
    (re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[_\-T](\d{2})(\d{2})(\d{2})" + _SUBSEC), SECOND_PRECISION),
    # 2023-04-15-10-30-00, 2023-04-15 10.30.00, 2023_04_15_10_30_00
    (re.compile(
        r"(?<!\d)(\d{4})[-_.](\d{2})[-_.](\d{2})[ _\-T](\d{2})[-_.:](\d{2})[-_.:](\d{2})(?!\d)"
    ), SECOND_PRECISION),
    # signal-2023-04-15-103000 — dashed date, then an unseparated time
    (re.compile(
        r"(?<!\d)(\d{4})[-_.](\d{2})[-_.](\d{2})[ _\-T](\d{2})(\d{2})(\d{2})" + _SUBSEC
    ), SECOND_PRECISION),
    # 2023-04-15, 2023_04_15
    (re.compile(r"(?<!\d)(\d{4})[-_.](\d{2})[-_.](\d{2})(?!\d)"), DAY_PRECISION),
    # 20230415 (IMG-20230415-WA0001, and friends)
    (re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"), DAY_PRECISION),
)

# Album names like "2023-04 Trip" or "2023_04". Month precision only.
_ALBUM_PATTERN = re.compile(r"(?<!\d)(\d{4})[-_.](\d{2})(?!\d)")

# exiftool -j renders these as "2023:04:15 10:30:00", sometimes with a
# fractional part and/or a UTC offset appended.
_EXIF_PATTERN = re.compile(
    r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
)

# Preference order when reading what the downloaded file actually carries.
# DateTimeOriginal is the real capture time; the rest are progressively
# weaker stand-ins that Google either ignores or never looked at.
EXIF_DATE_TAGS = (
    "DateTimeOriginal",
    "SubSecDateTimeOriginal",
    "CreateDate",
    "SubSecCreateDate",
    "QuickTime:CreateDate",
    "TrackCreateDate",
    "MediaCreateDate",
    "GPSDateTime",
    "ModifyDate",
)


def _build(parts: tuple[str, ...]) -> datetime | None:
    """Turn regex groups into a datetime, or None if they aren't a real one."""
    try:
        numbers = [int(part) for part in parts]
    except (TypeError, ValueError):
        return None
    if len(numbers) == 3:
        numbers += [0, 0, 0]
    if len(numbers) != 6:
        return None
    try:
        parsed = datetime(*numbers)
    except ValueError:
        return None
    return parsed if in_sane_range(parsed) else None


def in_sane_range(value: datetime, *, now: datetime | None = None) -> bool:
    """Reject dates that predate digital photography or sit in the future."""
    if value.year < MIN_YEAR:
        return False
    reference = now or datetime.now()
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    if reference.tzinfo is not None:
        reference = reference.astimezone(timezone.utc).replace(tzinfo=None)
    return value <= reference + FUTURE_SLACK


def parse_filename_date(filename: str) -> datetime | None:
    """Extract a capture timestamp from common camera/messenger filenames.

    Handles IMG_/VID_/PXL_/Screenshot_ prefixes, WhatsApp's
    IMG-20230415-WA0001, Signal's signal-2023-04-15-103000, and bare
    timestamps, in that order of specificity. Returns None when nothing
    date-shaped and plausible is present.
    """
    return parse_filename_date_with_precision(filename)[0]


def parse_filename_date_with_precision(filename: str) -> tuple[datetime | None, str]:
    """As parse_filename_date, but also says how precise the match was."""
    if not filename:
        return None, ""
    for pattern, precision in _FILENAME_PATTERNS:
        for match in pattern.finditer(filename):
            parsed = _build(match.groups())
            if parsed is not None:
                return parsed, precision
    return None, ""


def tolerance_for(precision: str) -> timedelta:
    return DAY_PRECISION_TOLERANCE if precision == DAY_PRECISION else MISDATE_TOLERANCE


def parse_album_date(album: str) -> datetime | None:
    """Month-precision fallback for albums named like '2023-04 Trip'."""
    if not album:
        return None
    match = _ALBUM_PATTERN.search(album)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    try:
        parsed = datetime(year, month, 1)
    except ValueError:
        return None
    return parsed if in_sane_range(parsed) else None


def parse_exif_datetime(value: str) -> datetime | None:
    """Parse one exiftool date string. Zero dates ('0000:00:00') yield None."""
    if not value:
        return None
    match = _EXIF_PATTERN.match(str(value).strip())
    if not match:
        return None
    return _build(match.groups())


def embedded_date(tags: dict) -> tuple[datetime | None, str]:
    """Best capture date the file itself carries, and which tag supplied it."""
    if not tags:
        return None, ""
    for name in EXIF_DATE_TAGS:
        parsed = parse_exif_datetime(tags.get(name) or "")
        if parsed is not None:
            return parsed, name
    return None, ""


def parse_creation_time(value: str) -> datetime | None:
    """Parse the API's mediaMetadata.creationTime (RFC 3339, UTC)."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def is_misdated(
    creation: datetime | None,
    resolved: datetime | None,
    *,
    tolerance: timedelta = MISDATE_TOLERANCE,
) -> bool:
    """True when Google's date is too late, by more than `tolerance`.

    Deliberately one-directional. Google's creationTime is either the real
    capture time or the moment of upload, and both are at or after the
    capture — so a genuine capture date is never *later* than what Google
    holds. A candidate date that sits after Google's is therefore evidence
    the filename is lying, not that the item is mis-dated: export tools
    stamp their own run date into names like IMG_2026-05-13_23-57-18.jpg
    over a photo actually taken in 2014. Treating those as fixes would
    rewrite correct dates with wrong ones.
    """
    if creation is None or resolved is None:
        return False
    delta = _naive_utc(creation) - _naive_utc(resolved)
    return delta > tolerance


def resolve_date(
    *,
    filename: str,
    tags: dict | None = None,
    album: str = "",
) -> tuple[datetime | None, str]:
    """Work out an item's true capture date and say where it came from.

    Priority, highest first:
      1. metadata actually embedded in the file — direct evidence, and it
         wins over a filename guess whenever the two disagree;
      2. a timestamp in the filename — the strongest signal available before
         downloading, and exactly what the EXIF-stripped files carry;
      3. a date in the album name — month precision, last resort.

    File mtime is deliberately not consulted: these files are downloaded
    from the cloud, so their mtime is the download time and says nothing.
    """
    embedded, tag_name = embedded_date(tags or {})
    if embedded is not None:
        return embedded, f"exif:{tag_name}"
    from_name = parse_filename_date(filename)
    if from_name is not None:
        return from_name, "filename"
    from_album = parse_album_date(album)
    if from_album is not None:
        return from_album, "album"
    return None, ""


def is_video(mime_type: str, filename: str = "") -> bool:
    if str(mime_type or "").lower().startswith(VIDEO_MIME_PREFIX):
        return True
    return filename.lower().endswith((".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp"))


def sniff_image_format(head: bytes) -> str:
    """Return png/jpeg/gif/webp from the first bytes, or '' if unknown."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return ""


def sniff_image_format_path(path) -> str:
    try:
        with open(path, "rb") as handle:
            return sniff_image_format(handle.read(16))
    except OSError:
        return ""


def exiftool_extension_for_format(image_format: str) -> str:
    return {
        "png": ".png",
        "jpeg": ".jpg",
        "gif": ".gif",
        "webp": ".webp",
    }.get(image_format, "")


def exiftool_args_for(
    mime_type: str,
    value: datetime,
    filename: str = "",
    *,
    image_format: str = "",
) -> list[str]:
    """exiftool arguments that write `value` into the tags Google reads.

    Videos need the QuickTime tags (and QuickTimeUTC, since those are
    defined as UTC); stills need the EXIF trio or PNG/WebP equivalents.
    Pass `image_format` from sniff_image_format when the bytes disagree
    with the filename — common for PNG screenshots named .jpg.
    """
    stamp = value.strftime("%Y:%m:%d %H:%M:%S")
    if is_video(mime_type, filename):
        return [
            "-api", "QuickTimeUTC=1",
            f"-QuickTime:CreateDate={stamp}",
            f"-QuickTime:ModifyDate={stamp}",
            f"-QuickTime:TrackCreateDate={stamp}",
            f"-QuickTime:MediaCreateDate={stamp}",
        ]
    fmt = (image_format or "").lower()
    if fmt == "png":
        return [
            f"-PNG:CreationTime={stamp}",
            f"-PNG:ModifyDate={stamp}",
        ]
    if fmt == "webp":
        return [
            f"-WebP:CreateDate={stamp}",
            f"-WebP:ModifyDate={stamp}",
        ]
    if fmt == "gif":
        return [f"-GIF:Time={stamp}"]
    return [
        f"-EXIF:DateTimeOriginal={stamp}",
        f"-EXIF:CreateDate={stamp}",
        f"-EXIF:ModifyDate={stamp}",
    ]
