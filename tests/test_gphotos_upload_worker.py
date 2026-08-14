from __future__ import annotations

import unittest
from unittest.mock import patch

import gphotos_upload_worker as worker


class RcloneStopTests(unittest.TestCase):
    def test_run_rclone_terminates_child_when_stop_is_requested(self) -> None:
        class FakeProcess:
            returncode = None

            def __init__(self) -> None:
                self.terminated = False
                self.poll_count = 0

            def poll(self):
                self.poll_count += 1
                return 0 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        process = FakeProcess()
        with patch.object(worker.subprocess, "Popen", return_value=process), patch.object(
            worker.time, "sleep", return_value=None
        ):
            code = worker._run_rclone(["rclone", "copy"], lambda: process.poll_count >= 1)

        self.assertEqual(code, 130)
        self.assertTrue(process.terminated)


if __name__ == "__main__":
    unittest.main()
