from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gphotos_upload_common as common
from gphotos_upload_common import (
    atomic_write_json,
    count_media_entries,
    has_runnable_sources,
    merge_progress_counts,
    mask_secret,
    list_source_media,
    read_json,
    read_worker_status,
    rotate_log,
    status_message,
    update_json_file,
    validate_source_mappings,
    write_worker_status,
)


class GPhotosUploadCommonTests(unittest.TestCase):
    def test_validate_source_mappings_rejects_nested_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parent = root / "Pictures"
            nested = parent / "DCIM"
            duplicate = root / "PicturesCopy"
            parent.mkdir()
            nested.mkdir()
            duplicate.mkdir()

            normalized, errors = validate_source_mappings(
                [
                    {"path": str(parent), "album": "Parent"},
                    {"path": str(nested), "album": "Nested"},
                    {"path": str(parent), "album": "Duplicate"},
                    {"path": str(duplicate), "album": "Copy"},
                ]
            )

            self.assertEqual([item["path"] for item in normalized], [str(parent.resolve()), str(duplicate.resolve())])
            self.assertTrue(any("nested sources are not allowed" in err for err in errors))
            self.assertTrue(any("duplicate folder" in err for err in errors))

    def test_validate_source_mappings_rejects_missing_album_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            folder = root / "Folder"
            folder.mkdir()

            normalized, errors = validate_source_mappings([{ "path": str(folder), "album": "   " }])

            self.assertEqual(normalized, [])
            self.assertTrue(errors)

    def test_validate_source_mappings_preserves_pause_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            folder = root / "Folder"
            folder.mkdir()

            normalized, errors = validate_source_mappings(
                [{"path": str(folder), "album": "Folder", "paused": True, "cancelled": True}]
            )

            self.assertEqual(errors, [])
            self.assertEqual(normalized[0]["paused"], True)
            self.assertEqual(normalized[0]["cancelled"], True)

    def test_has_runnable_sources_ignores_paused_and_cancelled_rows(self) -> None:
        self.assertTrue(
            has_runnable_sources(
                [
                    {"path": "/a", "album": "A", "paused": True, "cancelled": False},
                    {"path": "/b", "album": "B", "paused": False, "cancelled": False},
                ]
            )
        )
        self.assertFalse(
            has_runnable_sources(
                [
                    {"path": "/a", "album": "A", "paused": True, "cancelled": False},
                    {"path": "/b", "album": "B", "paused": True, "cancelled": True},
                ]
            )
        )

    def test_count_media_entries_filters_supported_media_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "a.jpg").write_text("x", encoding="utf-8")
            (root / "b.mp4").write_text("x", encoding="utf-8")
            (root / "c.txt").write_text("x", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "d.webp").write_text("x", encoding="utf-8")

            images, videos, total = count_media_entries(root)

            self.assertEqual((images, videos, total), (2, 1, 3))

    def test_merge_progress_counts_uses_the_highest_bounded_value(self) -> None:
        self.assertEqual(merge_progress_counts(100, 0, None, "9"), 9)
        self.assertEqual(merge_progress_counts(100, 4, 12, 8), 12)
        self.assertEqual(merge_progress_counts(100, 120, 50), 100)

    def test_list_source_media_reuses_cached_sets_for_stable_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "one.jpg").write_text("x", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "clip.mp4").write_text("x", encoding="utf-8")

            with patch.object(common.time, "monotonic", return_value=100.0):
                images1, videos1 = list_source_media(root)
                images2, videos2 = list_source_media(root)

            self.assertEqual(images1, {"one.jpg"})
            self.assertEqual(videos1, {"nested/clip.mp4"})
            self.assertIs(images1, images2)
            self.assertIs(videos1, videos2)

    def test_update_json_file_is_atomic_under_contention(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "config.json"
            lock = root / "config.lock"
            atomic_write_json(target, {"count": 0})

            def bump() -> None:
                for _ in range(50):
                    def transform(current: dict) -> dict:
                        current["count"] = int(current.get("count") or 0) + 1
                        return current

                    update_json_file(target, transform, lock_path=lock, default={"count": 0})

            threads = [threading.Thread(target=bump) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(read_json(target, {}).get("count"), 400)

    def test_status_roundtrip_and_masking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            status_path = root / "upload.status"
            payload = {"phase": "completed", "message": "done", "error": "", "exit_code": 0}
            write_worker_status(status_path, payload)

            status = read_worker_status(status_path)

            self.assertEqual(status_message(status), "done")
            self.assertEqual(status["phase"], "completed")

    def test_mask_secret_redacts_sensitive_values(self) -> None:
        self.assertEqual(mask_secret(""), "Not configured")
        self.assertEqual(mask_secret("abcd1234"), "••••••••")
        self.assertEqual(mask_secret("super-secret-token"), "supe…oken")

    def test_rotate_log_backs_up_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log = root / "upload.log"
            log.write_text("x" * 1024, encoding="utf-8")

            rotate_log(log, backups=2, max_bytes=1)

            self.assertFalse(log.exists() and log.read_text(encoding="utf-8") == "x" * 1024)
            self.assertTrue((root / "upload.log.1").exists())


if __name__ == "__main__":
    unittest.main()
