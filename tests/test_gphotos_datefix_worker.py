"""Scan-pipeline coverage for the date-fix worker, with the API stubbed out.

Exercises the wiring end to end — album lookup, per-item triage, status file
— without touching a real Google account.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import gphotos_datefix_worker as worker  # noqa: E402


_UNSET = object()


def _item(item_id, filename, creation, mime="image/jpeg"):
    return {
        "id": item_id, "filename": filename, "baseUrl": f"https://example/{item_id}",
        "mimeType": mime, "productUrl": f"https://photos.example/{item_id}",
        "creationTime": creation,
    }


class RunScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.status_path = Path(self._tmp.name) / "datefix.status"
        for name, value in (("DATEFIX_STATUS_PATH", self.status_path),
                            # Keep test runs out of the real log file.
                            ("DATEFIX_LOG_PATH", Path(self._tmp.name) / "datefix.log"),
                            ("DATEFIX_ORIGINALS_LOG_PATH", Path(self._tmp.name) / "datefix-originals.jsonl")):
            patcher = patch.object(worker, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _status(self):
        return json.loads(self.status_path.read_text(encoding="utf-8"))

    def _run(self, items, albums=_UNSET, token="tok"):
        # Sentinel, not `or`: an empty album map is a meaningful case here.
        if albums is _UNSET:
            albums = {"Trip": {"id": "album-1", "count": len(items), "url": ""}}

        def _iter_album(_token, _album_id, **_kwargs):
            if items:
                yield items

        with patch.object(worker.logic, "_gphotos_access_token", return_value=token), \
             patch.object(worker.logic, "list_albums", return_value=albums), \
             patch.object(worker.logic, "iter_album_items", side_effect=_iter_album):
            code = worker.run_scan({})
        return code, self._status()

    def test_flags_only_items_google_dated_wrongly(self):
        code, status = self._run([
            # Uploaded today, but the filename says 2023 — the whole point.
            _item("a", "IMG_20230415_101500.jpg", "2026-08-18T09:00:00Z"),
            # Filename agrees with Google: leave it alone.
            _item("b", "IMG_20240101_120000.jpg", "2024-01-01T12:00:00Z"),
            # No date anywhere in the name: cannot be judged.
            _item("c", "holiday.jpg", "2026-08-18T09:00:00Z"),
        ])
        self.assertEqual(code, 0)
        self.assertTrue(status["ok"])
        self.assertTrue(status["finished"])
        self.assertEqual(status["scanned"], 3)
        self.assertEqual(status["undetermined"], 1)
        self.assertEqual([c["id"] for c in status["candidates"]], ["a"])

    def test_candidate_carries_what_apply_needs(self):
        _code, status = self._run([_item("a", "IMG_20230415_101500.jpg", "2026-08-18T09:00:00Z")])
        candidate = status["candidates"][0]
        self.assertEqual(candidate["album_id"], "album-1")
        self.assertEqual(candidate["album"], "Trip")
        self.assertEqual(candidate["proposed"], "2023-04-15T10:15:00")
        self.assertEqual(candidate["current"], "2026-08-18T09:00:00Z")
        self.assertEqual(candidate["source"], "filename")

    def test_scan_writes_nothing_but_status(self):
        # A scan must never upload or mutate; the only side effect is the file.
        with patch.object(worker, "_upload") as upload, patch.object(worker, "_download") as download:
            self._run([_item("a", "IMG_20230415_101500.jpg", "2026-08-18T09:00:00Z")])
        upload.assert_not_called()
        download.assert_not_called()

    def test_needs_no_configured_source_folders(self):
        # Albums come from the account; an empty local config is irrelevant.
        code, status = self._run([_item("a", "IMG_20230415_101500.jpg", "2026-08-18T09:00:00Z")])
        self.assertEqual(code, 0)
        self.assertEqual(status["albums_scanned"], 1)
        self.assertEqual(len(status["candidates"]), 1)

    def test_account_with_no_albums_is_not_fatal(self):
        code, status = self._run([], albums={})
        self.assertEqual(code, 0)
        self.assertEqual(status["albums_scanned"], 0)
        self.assertEqual(status["candidates"], [])

    def test_loose_library_items_are_not_scanned(self):
        # mediaItems.list is skipped on purpose: with appcreateddata it mostly
        # returns empty pages across the whole library and stalls the UI.
        code, status = self._run([], albums={})
        self.assertEqual(code, 0)
        self.assertEqual(status["candidates"], [])

    def test_album_pass_reports_progress_between_pages(self):
        pages = [
            [_item("a", "IMG_20230415_101500.jpg", "2026-08-18T09:00:00Z")],
            [_item("b", "IMG_20190101_120000.jpg", "2026-08-18T09:00:00Z")],
        ]
        phases: list[str] = []

        def _iter(_token, _album_id, **_kwargs):
            yield from pages

        real_write = worker._write_status

        def _capture(**fields):
            if fields.get("phase"):
                phases.append(fields["phase"])
            return real_write(**fields)

        albums = {"Trip": {"id": "album-1", "count": 2, "url": ""}}
        with patch.object(worker.logic, "_gphotos_access_token", return_value="tok"), \
             patch.object(worker.logic, "list_albums", return_value=albums), \
             patch.object(worker.logic, "iter_album_items", side_effect=_iter), \
             patch.object(worker, "_write_status", side_effect=_capture):
            code = worker.run_scan({})
        self.assertEqual(code, 0)
        self.assertTrue(any(p.startswith("Scanning Trip") for p in phases), phases)
        # Progress writes omit the candidate payload so large albums stay cheap.
        self.assertNotIn("Scanning items outside albums…", phases)

    def test_progress_status_omits_candidate_payload(self):
        written: list[dict] = []
        real_write = worker._write_status

        def _capture(**fields):
            written.append(dict(fields))
            return real_write(**fields)

        with patch.object(worker, "_write_status", side_effect=_capture):
            self._run([_item("a", "IMG_20230415_101500.jpg", "2026-08-18T09:00:00Z")])
        progress = [w for w in written if not w.get("finished")]
        self.assertTrue(progress)
        for entry in progress:
            self.assertNotIn("candidates", entry)
        final = [w for w in written if w.get("finished")][-1]
        self.assertIn("candidates", final)
        self.assertEqual(len(final["candidates"]), 1)

    def test_absent_token_fails_loudly(self):
        code, status = self._run([], token="")
        self.assertEqual(code, 1)
        self.assertFalse(status["ok"])
        self.assertIn("access token", status["error"])

    def test_timezone_skew_is_not_flagged(self):
        # Same local day, stored as UTC — must not be treated as mis-dated.
        _code, status = self._run([_item("a", "IMG_20230415_233000.jpg", "2023-04-16T02:30:00Z")])
        self.assertEqual(status["candidates"], [])


if __name__ == "__main__":
    unittest.main()


class _ApplyHarness:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.status_path = Path(self._tmp.name) / "datefix.status"
        for name, value in (("DATEFIX_STATUS_PATH", self.status_path),
                            # Keep test runs out of the real log file.
                            ("DATEFIX_LOG_PATH", Path(self._tmp.name) / "datefix.log"),
                            ("DATEFIX_ORIGINALS_LOG_PATH", Path(self._tmp.name) / "datefix-originals.jsonl")):
            patcher = patch.object(worker, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _status(self):
        return json.loads(self.status_path.read_text(encoding="utf-8"))

    def _candidate(self, item_id="a", filename="IMG_20230415_101500.jpg"):
        return {
            "id": item_id, "album": "Trip", "album_id": "album-1",
            "filename": filename, "mimeType": "image/jpeg",
            "productUrl": "", "current": "2026-08-18T09:00:00Z",
            "proposed": "2023-04-15T10:15:00", "source": "filename",
        }

    def _run(self, candidates, *, tags=None, upload_ok=True, write_ok=True, fresh=None, max_items=500):
        if fresh is None:
            fresh = [_item(c["id"], c["filename"], c["current"]) for c in candidates]
        self.removed = []
        self.corralled = []

        def record_remove(_token, album_id, ids):
            self.removed.append((album_id, list(ids)))
            return len(ids)

        def record_add(_token, album_id, ids):
            self.corralled.append((album_id, list(ids)))
            return list(ids)

        with patch.object(worker.logic, "_gphotos_access_token", return_value="tok"), \
             patch.object(worker.logic, "search_album_items", return_value=fresh), \
             patch.object(worker.logic, "list_library_items", return_value=fresh), \
             patch.object(worker.logic, "album_remove_items", side_effect=record_remove), \
             patch.object(worker.logic, "find_or_create_album", return_value={"id": "cleanup-1", "url": "https://photos/cleanup"}), \
             patch.object(worker.logic, "album_add_items", side_effect=record_add), \
             patch.object(worker, "_download"), \
             patch.object(worker, "_read_tags", return_value=tags or {}), \
             patch.object(worker, "_write_date", return_value=write_ok) as self.write_date, \
             patch.object(worker, "_upload", return_value=upload_ok) as self.upload:
            code = worker.run_apply({
                "candidates": candidates,
                "max_items": max_items,
                # Keep unit tests on the serial path so existing _upload mocks apply.
                "prepare_workers": 1,
                "upload_transfers": 1,
                "chunk_size": max(1, len(candidates) or 1),
            })
        return code, self._status()


class RunApplyTests(_ApplyHarness, unittest.TestCase):
    def test_fixes_then_corrals_and_removes_the_original(self):
        code, status = self._run([self._candidate()])
        self.assertEqual(code, 0)
        self.assertEqual(status["fixed"], 1)
        self.assertEqual(status["failed"], 0)
        self.assertEqual(self.corralled, [("cleanup-1", ["a"])])
        self.assertEqual(self.removed, [("album-1", ["a"])])
        self.assertEqual(status["corralled"], 1)
        self.assertEqual(status["cleanup_url"], "https://photos/cleanup")
        self.assertIn("delete them", status["phase"])
        # The date written is the one the filename implies.
        self.assertEqual(self.write_date.call_args[0][1].isoformat(), "2023-04-15T10:15:00")

    def test_fixed_original_is_logged_to_jsonl(self):
        originals_path = Path(worker.DATEFIX_ORIGINALS_LOG_PATH)
        self._run([self._candidate()])
        lines = originals_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        superseded = json.loads(lines[0])
        corralled = json.loads(lines[1])
        self.assertEqual(superseded["event"], "original_superseded")
        self.assertEqual(superseded["original"]["id"], "a")
        self.assertEqual(superseded["original"]["filename"], "IMG_20230415_101500.jpg")
        self.assertEqual(superseded["corrected"]["date"], "2023-04-15T10:15:00")
        self.assertEqual(corralled["event"], "original_corralled")
        self.assertEqual(corralled["original_id"], "a")
        self.assertTrue(corralled["removed_from_album"])

    def test_original_survives_a_failed_upload(self):
        # Removing it here would lose the only copy — the whole safety story.
        _code, status = self._run([self._candidate()], upload_ok=False)
        self.assertEqual(status["fixed"], 0)
        self.assertEqual(status["failed"], 1)
        self.assertEqual(self.removed, [])

    def test_original_survives_a_failed_retag(self):
        _code, status = self._run([self._candidate()], write_ok=False)
        self.assertEqual(status["failed"], 1)
        self.upload.assert_not_called()
        self.assertEqual(self.removed, [])

    def test_skips_when_embedded_metadata_says_google_is_right(self):
        # Filename guess was wrong: the file itself agrees with Google.
        _code, status = self._run(
            [self._candidate()], tags={"DateTimeOriginal": "2026:08:18 09:00:00"}
        )
        self.assertEqual(status["fixed"], 0)
        self.assertEqual(status["skipped"], 1)
        self.upload.assert_not_called()
        self.assertEqual(self.removed, [])

    def test_embedded_metadata_overrides_the_filename_guess(self):
        _code, status = self._run(
            [self._candidate()], tags={"DateTimeOriginal": "2019:01:02 03:04:05"}
        )
        self.assertEqual(status["fixed"], 1)
        self.assertEqual(self.write_date.call_args[0][1].isoformat(), "2019-01-02T03:04:05")

    def test_png_bytes_with_jpg_name_can_be_retagged(self):
        import base64
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        candidate = self._candidate(filename="IMG_2020-05-24_10-43-32.jpg")
        fresh = [_item("a", candidate["filename"], candidate["current"])]

        def fake_download(_url, destination, *, video=False):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(png)

        with patch.object(worker.logic, "_gphotos_access_token", return_value="tok"), \
             patch.object(worker.logic, "search_album_items", return_value=fresh), \
             patch.object(worker.logic, "list_library_items", return_value=[]), \
             patch.object(worker.logic, "album_remove_items", return_value=1), \
             patch.object(worker.logic, "find_or_create_album",
                          return_value={"id": "cleanup-1", "url": "https://photos/cleanup"}), \
             patch.object(worker.logic, "album_add_items", return_value=["a"]), \
             patch.object(worker, "_download", side_effect=fake_download), \
             patch.object(worker, "_read_tags", return_value={}), \
             patch.object(worker, "_upload", return_value=True) as upload:
            code = worker.run_apply({
                "candidates": [candidate], "max_items": 10,
                "prepare_workers": 1, "upload_transfers": 1, "chunk_size": 8,
            })
        status = self._status()
        self.assertEqual(code, 0)
        self.assertEqual(status["fixed"], 1)
        self.assertEqual(status["failed"], 0)
        upload.assert_called_once()

    def test_item_gone_from_album_is_a_failure_not_a_crash(self):
        _code, status = self._run([self._candidate()], fresh=[])
        self.assertEqual(status["failed"], 1)
        self.assertEqual(self.removed, [])

    def test_loose_item_is_uploaded_but_never_album_removed(self):
        candidate = self._candidate()
        candidate["album"] = ""
        candidate["album_id"] = ""
        _code, status = self._run([candidate])
        self.assertEqual(status["fixed"], 1)
        self.assertEqual(self.upload.call_args[0][1], "")
        self.assertEqual(self.removed, [])
        # It has no album to leave, but still needs collecting for deletion.
        self.assertEqual(self.corralled, [("cleanup-1", ["a"])])

    def test_album_item_is_uploaded_to_its_own_album(self):
        candidate = self._candidate()
        candidate["album"] = "Trip 2023"
        _code, status = self._run([candidate])
        self.assertEqual(status["fixed"], 1)
        self.assertEqual(self.upload.call_args[0][1], "Trip 2023")
        self.assertEqual(self.removed, [("album-1", ["a"])])

    def test_reports_leftovers_beyond_the_cap(self):
        candidates = [self._candidate(f"id{n}") for n in range(3)]
        _code, status = self._run(candidates, max_items=2)
        self.assertEqual(status["total"], 2)
        self.assertEqual(status["fixed"], 2)
        self.assertEqual(status["remaining"], 1)


class UploadDestinationTests(unittest.TestCase):
    """The corrected copy must land where the original was."""

    def _destination(self, album):
        with patch.object(worker.subprocess, "run") as run:
            run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
            worker._upload(Path("/tmp/x.jpg"), album)
        command = run.call_args[0][0]
        return command[command.index("copy") + 2]

    def test_album_member_goes_back_to_its_album(self):
        self.assertEqual(self._destination("Trip 2023"), "gphotos:album/Trip 2023")

    def test_loose_item_goes_back_to_the_library(self):
        self.assertEqual(self._destination(""), "gphotos:upload")


class SafeNameTests(unittest.TestCase):
    """rclone names the uploaded item after the local file, so this name is
    the one that ends up showing in Google Photos."""

    def test_ordinary_names_are_preserved_exactly(self):
        for name in ["VID-20180911-WA0026.mp4", "IMG_2023 (1).HEIC", "Screenshot_2023-04-15.png"]:
            with self.subTest(name=name):
                self.assertEqual(worker._safe_name(name), name)

    def test_path_separators_cannot_escape_the_work_dir(self):
        self.assertEqual(worker._safe_name("../../etc/passwd.jpg"), ".._.._etc_passwd.jpg")
        self.assertNotIn("/", worker._safe_name("a/b/c.jpg"))

    def test_degenerate_names_get_a_placeholder(self):
        for name in ["", ".", "..", "   "]:
            with self.subTest(name=name):
                self.assertEqual(worker._safe_name(name), "item.bin")

    def test_overlong_names_keep_their_extension(self):
        result = worker._safe_name("x" * 300 + ".jpeg")
        self.assertTrue(result.endswith(".jpeg"))
        self.assertLessEqual(len(result), 120)


class ListingCancellationTests(unittest.TestCase):
    """Cancel must not have to wait out a multi-thousand-item listing."""

    def setUp(self):
        self.pages = 0

    def _page(self, *_args, **_kwargs):
        self.pages += 1
        return {
            "mediaItems": [{"id": f"i{self.pages}", "filename": "x.jpg",
                            "mediaMetadata": {"creationTime": "2026-01-01T00:00:00Z"}}],
            "nextPageToken": "more",
        }

    def test_album_search_stops_between_pages(self):
        with patch.object(worker.logic, "_api_request", side_effect=self._page):
            items = worker.logic.search_album_items("tok", "album-1", should_stop=lambda: self.pages >= 3)
        self.assertEqual(self.pages, 3)
        self.assertEqual(len(items), 3)

    def test_album_iter_stops_between_pages(self):
        with patch.object(worker.logic, "_api_request", side_effect=self._page):
            pages = list(worker.logic.iter_album_items(
                "tok", "album-1", should_stop=lambda: self.pages >= 3,
            ))
        self.assertEqual(self.pages, 3)
        self.assertEqual(sum(len(p) for p in pages), 3)

    def test_library_listing_stops_between_pages(self):
        with patch.object(worker.logic, "_api_request", side_effect=self._page):
            items = worker.logic.list_library_items("tok", should_stop=lambda: self.pages >= 2)
        self.assertEqual(self.pages, 2)
        self.assertEqual(len(items), 2)

    def test_without_should_stop_it_pages_to_the_limit(self):
        with patch.object(worker.logic, "_api_request", side_effect=self._page):
            items = worker.logic.list_library_items("tok", limit=5)
        self.assertEqual(len(items), 5)


class CleanupAlbumTests(_ApplyHarness, unittest.TestCase):
    """The original must never be stranded: corral first, then unfile."""

    def test_original_stays_in_its_album_if_corralling_fails(self):
        def boom(*_args, **_kwargs):
            raise OSError("batchAddMediaItems refused")

        candidate = self._candidate()
        with patch.object(worker.logic, "_gphotos_access_token", return_value="tok"), \
             patch.object(worker.logic, "search_album_items",
                          return_value=[_item("a", candidate["filename"], candidate["current"])]), \
             patch.object(worker.logic, "list_library_items", return_value=[]), \
             patch.object(worker.logic, "find_or_create_album", side_effect=boom), \
             patch.object(worker.logic, "album_add_items", side_effect=boom), \
             patch.object(worker.logic, "album_remove_items") as remove, \
             patch.object(worker, "_download"), \
             patch.object(worker, "_read_tags", return_value={}), \
             patch.object(worker, "_write_date", return_value=True), \
             patch.object(worker, "_upload", return_value=True):
            worker.run_apply({
                "candidates": [candidate], "max_items": 500,
                "prepare_workers": 1, "upload_transfers": 1,
            })
        status = self._status()
        self.assertEqual(status["fixed"], 1)
        self.assertEqual(status["corralled"], 0)
        remove.assert_not_called()
        # Falls back to telling the user they are loose in the timeline.
        self.assertIn("still in your timeline", status["phase"])

    def test_nothing_fixed_means_no_album_is_created(self):
        # The item is skipped (its own metadata backs Google's date), so
        # there are no originals to collect and no album to make.
        candidate = self._candidate()
        with patch.object(worker.logic, "_gphotos_access_token", return_value="tok"), \
             patch.object(worker.logic, "search_album_items",
                          return_value=[_item("a", candidate["filename"], candidate["current"])]), \
             patch.object(worker.logic, "list_library_items", return_value=[]), \
             patch.object(worker.logic, "find_or_create_album") as create, \
             patch.object(worker.logic, "album_add_items") as add, \
             patch.object(worker, "_download"), \
             patch.object(worker, "_read_tags", return_value={"DateTimeOriginal": "2026:08:18 09:00:00"}), \
             patch.object(worker, "_write_date", return_value=True), \
             patch.object(worker, "_upload", return_value=True):
            worker.run_apply({
                "candidates": [candidate], "max_items": 500,
                "prepare_workers": 1, "upload_transfers": 1,
            })
        status = self._status()
        self.assertEqual(status["skipped"], 1)
        self.assertEqual(status["fixed"], 0)
        create.assert_not_called()
        add.assert_not_called()


class UploadManyTests(unittest.TestCase):
    """Parallel rclone uploads stage into a directory then fall back safely."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(worker, "DATEFIX_LOG_PATH", Path(self._tmp.name) / "datefix.log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_serial_path_calls_upload_per_file(self):
        paths = [Path(self._tmp.name) / "a.jpg", Path(self._tmp.name) / "b.jpg"]
        for path in paths:
            path.write_bytes(b"x")
        with patch.object(worker, "_upload", side_effect=[True, False]) as upload:
            ok = worker._upload_many(paths, "Trip", transfers=1)
        self.assertEqual(ok, {paths[0]})
        self.assertEqual(upload.call_count, 2)

    def test_parallel_path_uses_one_rclone_for_the_batch(self):
        paths = [Path(self._tmp.name) / "a.jpg", Path(self._tmp.name) / "b.jpg"]
        for path in paths:
            path.write_bytes(b"x")

        def fake_run(command, **_kwargs):
            self.assertEqual(command[0], "rclone")
            self.assertIn("--transfers", command)
            self.assertEqual(command[command.index("--transfers") + 1], "4")
            # Staging dir is the source argument.
            self.assertTrue(Path(command[2]).is_dir())
            return unittest.mock.Mock(returncode=0, stdout="", stderr="")

        with patch.object(worker.subprocess, "run", side_effect=fake_run) as run:
            ok = worker._upload_many(paths, "Trip", transfers=4)
        self.assertEqual(ok, set(paths))
        self.assertEqual(run.call_count, 1)

    def test_parallel_failure_falls_back_to_serial(self):
        paths = [Path(self._tmp.name) / "a.jpg", Path(self._tmp.name) / "b.jpg"]
        for path in paths:
            path.write_bytes(b"x")
        with patch.object(worker.subprocess, "run",
                          return_value=unittest.mock.Mock(returncode=1, stdout="", stderr="nope")), \
             patch.object(worker, "_upload", return_value=True) as upload:
            ok = worker._upload_many(paths, "Trip", transfers=4)
        self.assertEqual(ok, set(paths))
        self.assertEqual(upload.call_count, 2)


class ParallelApplyTests(_ApplyHarness, unittest.TestCase):
    def test_two_items_same_album_upload_together(self):
        candidates = [
            self._candidate("a", "IMG_20230415_101500.jpg"),
            self._candidate("b", "IMG_20190101_120000.jpg"),
        ]
        fresh = [_item(c["id"], c["filename"], c["current"]) for c in candidates]
        uploaded: list[tuple] = []

        def record_many(paths, album, *, transfers=1):
            uploaded.append((list(paths), album, transfers))
            return set(paths)

        with patch.object(worker.logic, "_gphotos_access_token", return_value="tok"), \
             patch.object(worker.logic, "search_album_items", return_value=fresh), \
             patch.object(worker.logic, "list_library_items", return_value=[]), \
             patch.object(worker.logic, "album_remove_items", return_value=2), \
             patch.object(worker.logic, "find_or_create_album",
                          return_value={"id": "cleanup-1", "url": "https://photos/cleanup"}), \
             patch.object(worker.logic, "album_add_items", side_effect=lambda *_a, **_k: ["a", "b"]), \
             patch.object(worker, "_download"), \
             patch.object(worker, "_read_tags", return_value={}), \
             patch.object(worker, "_write_date", return_value=True), \
             patch.object(worker, "_upload_many", side_effect=record_many):
            code = worker.run_apply({
                "candidates": candidates, "max_items": 10,
                "prepare_workers": 2, "upload_transfers": 4, "chunk_size": 8,
            })
        status = self._status()
        self.assertEqual(code, 0)
        self.assertEqual(status["fixed"], 2)
        self.assertEqual(len(uploaded), 1)
        paths, album, transfers = uploaded[0]
        self.assertEqual(album, "Trip")
        self.assertEqual(transfers, 4)
        self.assertEqual(len(paths), 2)

    def test_export_stamped_name_is_skipped_not_applied(self):
        # IMG_2026-05-13_... over a 2014 photo: the name is later than
        # Google's date, so it cannot be a capture time.
        candidate = self._candidate(filename="IMG_2026-05-13_23-57-18.jpg")
        candidate["current"] = "2014-06-20T16:37:42Z"
        candidate["proposed"] = "2026-05-13T23:57:18"
        _code, status = self._run([candidate])
        self.assertEqual(status["fixed"], 0)
        self.assertEqual(status["skipped"], 1)
        self.upload.assert_not_called()
        self.assertEqual(self.corralled, [])

    def test_whatsapp_name_near_googles_date_is_skipped(self):
        candidate = self._candidate(filename="VID-20180924-WA0000.mp4")
        candidate["current"] = "2018-09-22T23:28:16Z"
        candidate["proposed"] = "2018-09-24T00:00:00"
        _code, status = self._run([candidate])
        self.assertEqual(status["skipped"], 1)
        self.upload.assert_not_called()

    def test_genuine_upload_time_fallback_is_still_fixed(self):
        candidate = self._candidate(filename="VID-20180911-WA0026.mp4")
        candidate["current"] = "2026-08-11T00:21:06Z"
        candidate["proposed"] = "2018-09-11T00:00:00"
        _code, status = self._run([candidate])
        self.assertEqual(status["fixed"], 1)
        self.assertEqual(self.write_date.call_args[0][1].date().isoformat(), "2018-09-11")


class CleanupAlbumIsNotRescannedTests(unittest.TestCase):
    """Originals parked for deletion must never be re-fixed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.status_path = Path(self._tmp.name) / "datefix.status"
        for name, value in (("DATEFIX_STATUS_PATH", self.status_path),
                            ("DATEFIX_LOG_PATH", Path(self._tmp.name) / "datefix.log")):
            patcher = patch.object(worker, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _scan(self, album_items, loose=None):
        albums = {
            "Trip": {"id": "album-1", "count": 0, "url": ""},
            worker.CLEANUP_ALBUM: {"id": "cleanup-1", "count": 0, "url": ""},
        }

        def _iter_album(_token, album_id, **_kwargs):
            batch = album_items.get(album_id, [])
            if batch:
                yield batch

        with patch.object(worker.logic, "_gphotos_access_token", return_value="tok"), \
             patch.object(worker.logic, "list_albums", return_value=albums), \
             patch.object(worker.logic, "iter_album_items", side_effect=_iter_album):
            worker.run_scan({})
        return json.loads(self.status_path.read_text(encoding="utf-8"))

    def test_items_parked_for_deletion_are_not_candidates_again(self):
        parked = _item("old", "IMG_20230415_101500.jpg", "2026-08-18T09:00:00Z")
        status = self._scan({"album-1": [], "cleanup-1": [parked]})
        self.assertEqual(status["candidates"], [])
        self.assertEqual(status["scanned"], 0)

    def test_other_albums_are_still_scanned(self):
        live = _item("new", "IMG_20230415_101500.jpg", "2026-08-18T09:00:00Z")
        status = self._scan({"album-1": [live], "cleanup-1": []})
        self.assertEqual([c["id"] for c in status["candidates"]], ["new"])
