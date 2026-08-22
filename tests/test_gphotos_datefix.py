from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from gphotos_datefix import (  # noqa: E402
    DAY_PRECISION,
    DAY_PRECISION_TOLERANCE,
    SECOND_PRECISION,
    embedded_date,
    exiftool_args_for,
    exiftool_extension_for_format,
    in_sane_range,
    is_misdated,
    is_video,
    parse_album_date,
    parse_creation_time,
    parse_exif_datetime,
    parse_filename_date,
    parse_filename_date_with_precision,
    resolve_date,
    sniff_image_format,
    sniff_image_format_path,
    tolerance_for,
)


class ParseFilenameDateTests(unittest.TestCase):
    def test_camera_and_messenger_patterns(self):
        cases = {
            "IMG_20230415_123456.jpg": datetime(2023, 4, 15, 12, 34, 56),
            "VID_20230415_123456.mp4": datetime(2023, 4, 15, 12, 34, 56),
            "PXL_20230415_123456789.jpg": datetime(2023, 4, 15, 12, 34, 56),
            "20230415_123456.jpg": datetime(2023, 4, 15, 12, 34, 56),
            "Screenshot_2023-04-15-10-30-00.png": datetime(2023, 4, 15, 10, 30, 0),
            "2023-04-15 10.30.00.jpg": datetime(2023, 4, 15, 10, 30, 0),
            "IMG-20230415-WA0001.jpg": datetime(2023, 4, 15),
            "signal-2023-04-15-103000.jpg": datetime(2023, 4, 15, 10, 30, 0),
            "2023-04-15.jpg": datetime(2023, 4, 15),
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(parse_filename_date(filename), expected)

    def test_timestamp_wins_over_bare_date(self):
        # The bare-date pattern must not claim a filename that carries a full
        # timestamp, or we would silently drop the time component.
        self.assertEqual(
            parse_filename_date("IMG_20230415_235959.jpg"),
            datetime(2023, 4, 15, 23, 59, 59),
        )

    def test_rejects_non_dates(self):
        for filename in [
            "",
            "holiday.jpg",
            "IMG_1234.jpg",
            "IMG_99999999.jpg",          # impossible month/day
            "IMG_20231345.jpg",          # month 13, day 45
            "12345678901234.jpg",        # long digit run, not a timestamp
            "IMG_18500415.jpg",          # before MIN_YEAR
        ]:
            with self.subTest(filename=filename):
                self.assertIsNone(parse_filename_date(filename))

    def test_rejects_future_dates(self):
        future = datetime.now() + timedelta(days=400)
        self.assertIsNone(parse_filename_date(f"IMG_{future:%Y%m%d}.jpg"))


class SaneRangeTests(unittest.TestCase):
    def test_bounds(self):
        now = datetime(2026, 8, 18, 12, 0, 0)
        self.assertTrue(in_sane_range(datetime(1990, 1, 1), now=now))
        self.assertFalse(in_sane_range(datetime(1989, 12, 31), now=now))
        self.assertTrue(in_sane_range(datetime(2026, 8, 18, 23, 0), now=now))
        self.assertFalse(in_sane_range(datetime(2026, 8, 20), now=now))


class ExifParsingTests(unittest.TestCase):
    def test_parses_exiftool_formats(self):
        self.assertEqual(
            parse_exif_datetime("2023:04:15 10:30:00"), datetime(2023, 4, 15, 10, 30)
        )
        self.assertEqual(
            parse_exif_datetime("2023:04:15 10:30:00.123+03:00"),
            datetime(2023, 4, 15, 10, 30),
        )

    def test_rejects_zero_and_junk(self):
        for value in ["", "0000:00:00 00:00:00", "not a date"]:
            with self.subTest(value=value):
                self.assertIsNone(parse_exif_datetime(value))

    def test_embedded_date_prefers_original(self):
        tags = {
            "ModifyDate": "2026:08:18 09:00:00",
            "DateTimeOriginal": "2023:04:15 10:30:00",
        }
        value, tag = embedded_date(tags)
        self.assertEqual(value, datetime(2023, 4, 15, 10, 30))
        self.assertEqual(tag, "DateTimeOriginal")

    def test_embedded_date_falls_through_to_weaker_tags(self):
        value, tag = embedded_date({"ModifyDate": "2023:04:15 10:30:00"})
        self.assertEqual(value, datetime(2023, 4, 15, 10, 30))
        self.assertEqual(tag, "ModifyDate")

    def test_embedded_date_absent(self):
        self.assertEqual(embedded_date({}), (None, ""))
        self.assertEqual(embedded_date({"Make": "Canon"}), (None, ""))


class CreationTimeTests(unittest.TestCase):
    def test_parses_rfc3339(self):
        self.assertEqual(
            parse_creation_time("2026-08-18T09:12:33Z"),
            datetime(2026, 8, 18, 9, 12, 33, tzinfo=timezone.utc),
        )

    def test_assumes_utc_when_naive(self):
        parsed = parse_creation_time("2026-08-18T09:12:33")
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_rejects_junk(self):
        self.assertIsNone(parse_creation_time(""))
        self.assertIsNone(parse_creation_time("yesterday"))


class MisdatedTests(unittest.TestCase):
    def test_flags_wildly_wrong_dates(self):
        creation = parse_creation_time("2026-08-18T09:00:00Z")
        self.assertTrue(is_misdated(creation, datetime(2023, 4, 15, 10, 30)))

    def test_tolerates_timezone_skew_within_a_day(self):
        # Same calendar day locally, stored in UTC — must not be flagged.
        creation = parse_creation_time("2023-04-15T21:00:00Z")
        self.assertFalse(is_misdated(creation, datetime(2023, 4, 16, 0, 30)))

    def test_boundary_is_exclusive(self):
        creation = parse_creation_time("2023-04-16T10:30:00Z")
        self.assertFalse(is_misdated(creation, datetime(2023, 4, 15, 10, 30)))
        self.assertTrue(is_misdated(creation, datetime(2023, 4, 15, 10, 29)))

    def test_missing_values_are_never_misdated(self):
        self.assertFalse(is_misdated(None, datetime(2023, 4, 15)))
        self.assertFalse(is_misdated(parse_creation_time("2026-08-18T09:00:00Z"), None))


class PrecisionTests(unittest.TestCase):
    """A date-only filename is worth less than a full timestamp."""

    def test_precision_is_reported(self):
        cases = {
            "IMG_20230415_123456.jpg": SECOND_PRECISION,
            "signal-2023-04-15-103000.jpg": SECOND_PRECISION,
            "IMG-20230415-WA0001.jpg": DAY_PRECISION,
            "2023-04-15.jpg": DAY_PRECISION,
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(parse_filename_date_with_precision(filename)[1], expected)

    def test_day_precision_gets_a_wider_tolerance(self):
        self.assertEqual(tolerance_for(DAY_PRECISION), DAY_PRECISION_TOLERANCE)
        self.assertEqual(tolerance_for(SECOND_PRECISION), timedelta(hours=24))

    def test_whatsapp_name_does_not_override_a_nearby_precise_date(self):
        # VID-20180924-WA0000: the name says the 24th, Google holds a real
        # timestamp on the 22nd. Replacing that with midnight loses detail.
        creation = parse_creation_time("2018-09-22T23:28:16Z")
        proposed, precision = parse_filename_date_with_precision("VID-20180924-WA0000.mp4")
        self.assertFalse(is_misdated(creation, proposed, tolerance=tolerance_for(precision)))

    def test_day_precision_still_catches_a_real_miss(self):
        # Same name, but Google dated it to upload time years later.
        creation = parse_creation_time("2026-08-11T00:21:06Z")
        proposed, precision = parse_filename_date_with_precision("VID-20180924-WA0000.mp4")
        self.assertTrue(is_misdated(creation, proposed, tolerance=tolerance_for(precision)))


class DirectionalityTests(unittest.TestCase):
    """Google's date is never earlier than the real capture time."""

    def test_a_filename_date_after_googles_is_not_a_fix(self):
        # IMG_2026-05-13_23-57-18.jpg over a photo Google dates to 2014:
        # an export tool stamped its own run date into the name. Applying
        # it would rewrite a correct 2014 date with a wrong 2026 one.
        creation = parse_creation_time("2014-06-20T16:37:42Z")
        proposed, precision = parse_filename_date_with_precision("IMG_2026-05-13_23-57-18.jpg")
        self.assertEqual(proposed, datetime(2026, 5, 13, 23, 57, 18))
        self.assertFalse(is_misdated(creation, proposed, tolerance=tolerance_for(precision)))

    def test_an_earlier_filename_date_is_still_a_fix(self):
        creation = parse_creation_time("2026-08-11T00:21:06Z")
        self.assertTrue(is_misdated(creation, datetime(2018, 9, 11, 0, 0, 0)))


class ResolveDateTests(unittest.TestCase):
    def test_embedded_metadata_overrides_filename_guess(self):
        value, source = resolve_date(
            filename="IMG_20230415_123456.jpg",
            tags={"DateTimeOriginal": "2019:01:02 03:04:05"},
        )
        self.assertEqual(value, datetime(2019, 1, 2, 3, 4, 5))
        self.assertEqual(source, "exif:DateTimeOriginal")

    def test_filename_used_when_file_carries_nothing(self):
        value, source = resolve_date(filename="IMG_20230415_123456.jpg", tags={})
        self.assertEqual(value, datetime(2023, 4, 15, 12, 34, 56))
        self.assertEqual(source, "filename")

    def test_album_is_last_resort(self):
        value, source = resolve_date(filename="holiday.jpg", tags={}, album="2023-04 Trip")
        self.assertEqual(value, datetime(2023, 4, 1))
        self.assertEqual(source, "album")

    def test_undeterminable(self):
        value, source = resolve_date(filename="holiday.jpg", tags={}, album="Trip")
        self.assertIsNone(value)
        self.assertEqual(source, "")

    def test_album_date_parsing(self):
        self.assertEqual(parse_album_date("2023-04 Trip"), datetime(2023, 4, 1))
        self.assertEqual(parse_album_date("2023_11"), datetime(2023, 11, 1))
        self.assertIsNone(parse_album_date("2023-13"))
        self.assertIsNone(parse_album_date("Trip"))
        self.assertIsNone(parse_album_date(""))


class ExiftoolArgsTests(unittest.TestCase):
    def setUp(self):
        self.when = datetime(2023, 4, 15, 10, 30, 0)

    def test_image_tags(self):
        args = exiftool_args_for("image/jpeg", self.when, "IMG_1.jpg")
        self.assertIn("-EXIF:DateTimeOriginal=2023:04:15 10:30:00", args)
        self.assertIn("-EXIF:CreateDate=2023:04:15 10:30:00", args)
        self.assertNotIn("-api", args)

    def test_video_tags_use_quicktime_and_utc_api(self):
        args = exiftool_args_for("video/mp4", self.when, "VID_1.mp4")
        self.assertIn("-api", args)
        self.assertIn("QuickTimeUTC=1", args)
        self.assertIn("-QuickTime:CreateDate=2023:04:15 10:30:00", args)
        self.assertNotIn("-EXIF:DateTimeOriginal=2023:04:15 10:30:00", args)

    def test_extension_decides_when_mime_is_missing(self):
        self.assertTrue(is_video("", "clip.MOV"))
        self.assertFalse(is_video("", "photo.jpg"))
        args = exiftool_args_for("", self.when, "clip.mkv")
        self.assertIn("-QuickTime:CreateDate=2023:04:15 10:30:00", args)

    def test_png_tags(self):
        args = exiftool_args_for("image/jpeg", self.when, "IMG_1.jpg", image_format="png")
        self.assertIn("-PNG:CreationTime=2023:04:15 10:30:00", args)
        self.assertNotIn("-EXIF:DateTimeOriginal=2023:04:15 10:30:00", args)


class SniffImageFormatTests(unittest.TestCase):
    def test_png_jpeg_and_webp(self):
        self.assertEqual(sniff_image_format(b"\x89PNG\r\n\x1a\n" + b"x"), "png")
        self.assertEqual(sniff_image_format(b"\xff\xd8\xff" + b"x"), "jpeg")
        self.assertEqual(
            sniff_image_format(b"RIFFxxxxWEBP" + b"x"),
            "webp",
        )
        self.assertEqual(sniff_image_format(b"unknown"), "")

    def test_sniff_from_path(self):
        import base64
        import tempfile
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
            handle.write(png)
            path = Path(handle.name)
        try:
            self.assertEqual(sniff_image_format_path(path), "png")
            self.assertEqual(exiftool_extension_for_format("png"), ".png")
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()


class TokenExpiryTests(unittest.TestCase):
    """A stale cached token is why the API 401s; catch it before calling."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
        from gphotos_upload_service.logic import token_expired
        self.token_expired = token_expired
        self.now = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)

    def test_past_expiry_is_expired(self):
        self.assertTrue(self.token_expired({"expiry": "2026-08-14T09:58:53.853035048+00:00"}, now=self.now))

    def test_future_expiry_is_live(self):
        self.assertFalse(self.token_expired({"expiry": "2026-08-18T13:30:00+00:00"}, now=self.now))

    def test_imminent_expiry_counts_as_expired(self):
        # Inside the slack window: refresh rather than risk a mid-call death.
        self.assertFalse(self.token_expired({"expiry": "2026-08-18T12:05:00+00:00"}, now=self.now))
        self.assertTrue(self.token_expired({"expiry": "2026-08-18T12:00:30+00:00"}, now=self.now))

    def test_nanosecond_precision_is_tolerated(self):
        # rclone writes 9 fractional digits; older Pythons reject that.
        self.assertTrue(self.token_expired({"expiry": "2026-08-14T09:58:53.853035048+00:00"}, now=self.now))

    def test_unparseable_or_absent_expiry_is_not_expired(self):
        self.assertFalse(self.token_expired({}, now=self.now))
        self.assertFalse(self.token_expired({"expiry": "soon"}, now=self.now))
