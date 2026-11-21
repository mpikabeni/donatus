from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services import ffmpeg as FF


class MediaFileValidationTests(unittest.TestCase):
    def test_verify_media_file_rejects_incomplete_download(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "video.mkv")
            with open(path, "wb") as fh:
                fh.write(b"partial")

            with self.assertRaisesRegex(FF.MediaProbeError, "incomplete"):
                FF.verify_media_file(path, expected_size=100)

            self.assertEqual(FF.verify_media_file(path, expected_size=7), 7)

    def test_verify_media_file_rejects_empty_and_missing_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            empty = os.path.join(td, "empty.mkv")
            Path(empty).touch()
            with self.assertRaisesRegex(FF.MediaProbeError, "empty"):
                FF.verify_media_file(empty)
            with self.assertRaisesRegex(FF.MediaProbeError, "missing"):
                FF.verify_media_file(os.path.join(td, "missing.mkv"))

        self.assertEqual(FF.verify_media_file("https://example.invalid/video.mkv"), 0)

    def test_duration_parser_tolerates_malformed_empty_probe_data(self):
        self.assertEqual(
            FF._duration_from_probe_data(
                {"format": None, "streams": [None, "bad", {"tags": None}]},
                "broken.mkv",
            ),
            0,
        )


class MediaProbeTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "video.mkv")
        with open(self.path, "wb") as fh:
            fh.write(b"not-empty")
        FF._PROBE_CACHE.clear()

    async def asyncTearDown(self):
        FF._PROBE_CACHE.clear()
        self._tmp.cleanup()

    async def test_probe_streams_handles_empty_output_and_missing_mediainfo(self):
        probe_calls = 0

        async def empty_probe(_args):
            nonlocal probe_calls
            probe_calls += 1
            return None

        with (
            patch.object(FF, "_probe_json", side_effect=empty_probe),
            patch.object(FF, "_mediainfo_streams", new=AsyncMock(return_value=None)),
        ):
            streams = await FF.probe_streams(self.path)

        self.assertEqual(streams, {"video": [], "audio": [], "subtitle": []})
        self.assertEqual(probe_calls, 2)

    async def test_public_probe_helpers_enforce_overall_deadline(self):
        async def never_finishes(*_args, **_kwargs):
            await asyncio.Event().wait()

        old_timeout = FF.PROBE_OVERALL_TIMEOUT_SECONDS
        FF.PROBE_OVERALL_TIMEOUT_SECONDS = 0.02
        started = time.monotonic()
        try:
            cases = (
                ("_probe_streams_uncached", FF.probe_streams),
                ("_probe_duration_bounded", FF.probe_duration),
                ("_get_mediainfo_bounded", FF.get_mediainfo),
            )
            for target, public_helper in cases:
                FF._PROBE_CACHE.clear()
                with patch.object(FF, target, side_effect=never_finishes):
                    with self.assertRaisesRegex(FF.MediaProbeError, "overall limit"):
                        await public_helper(self.path)
        finally:
            FF.PROBE_OVERALL_TIMEOUT_SECONDS = old_timeout

        self.assertLess(time.monotonic() - started, 0.5)

    async def test_ffprobe_process_timeout_kills_and_reaps_process(self):
        class HangingProcess:
            returncode = None

            def __init__(self):
                self.killed = False
                self.waited = False

            async def communicate(self):
                await asyncio.Event().wait()

            def kill(self):
                self.killed = True

            async def wait(self):
                self.waited = True
                await asyncio.Event().wait()

        proc = HangingProcess()
        old_timeout = FF._PROBE_PROCESS_TIMEOUT_SECONDS
        old_reap_timeout = FF._PROBE_REAP_TIMEOUT_SECONDS
        FF._PROBE_PROCESS_TIMEOUT_SECONDS = 0.02
        FF._PROBE_REAP_TIMEOUT_SECONDS = 0.02
        started = time.monotonic()
        try:
            with patch.object(
                FF.asyncio,
                "create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ):
                with self.assertRaisesRegex(FF.MediaProbeError, "timed out"):
                    await FF._probe_json(["-show_streams", self.path])
        finally:
            FF._PROBE_PROCESS_TIMEOUT_SECONDS = old_timeout
            FF._PROBE_REAP_TIMEOUT_SECONDS = old_reap_timeout

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertLess(time.monotonic() - started, 0.2)

    async def test_missing_mediainfo_binary_uses_ffprobe_fallback(self):
        fallback = AsyncMock(return_value="ffprobe fallback text")
        with (
            patch.object(
                FF.asyncio,
                "create_subprocess_exec",
                new=AsyncMock(side_effect=FileNotFoundError),
            ),
            patch.object(FF, "_ffprobe_mediainfo_text", new=fallback),
        ):
            text = await FF.get_mediainfo(self.path)

        self.assertEqual(text, "ffprobe fallback text")
        fallback.assert_awaited_once_with(self.path)


class VideoProbeIntegrationSourceTests(unittest.TestCase):
    def test_video_plugin_validates_downloads_and_uses_terminal_edits(self):
        src = Path("plugins/video.py").read_text(encoding="utf-8")

        self.assertIn("FF.verify_media_file(cached_path, session.fsize)", src)
        self.assertIn("FF.verify_media_file(path, session.fsize)", src)
        self.assertIn("FF.verify_media_file(sec, sec_size)", src)
        self.assertIn("clear_edit_throttle(st)", src)
        self.assertIn("async def _important_status_edit", src)
        self.assertIn("timeout=FF.PROBE_OVERALL_TIMEOUT_SECONDS + 1.0", src)
        self.assertIn("No readable media streams were found", src)


if __name__ == "__main__":
    unittest.main()
