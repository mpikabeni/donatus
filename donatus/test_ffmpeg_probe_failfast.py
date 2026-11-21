import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services import ffmpeg as FF


class ProbeFailFastTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FF._PROBE_CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "video.mkv")
        with open(self.path, "wb") as fh:
            fh.write(b"complete-media")

    def tearDown(self):
        self.tmp.cleanup()
        FF._PROBE_CACHE.clear()

    def test_verify_media_file_rejects_truncated_download(self):
        with self.assertRaisesRegex(FF.MediaProbeError, "incomplete"):
            FF.verify_media_file(self.path, expected_size=100)

    async def test_probe_streams_handles_empty_or_malformed_stream_output(self):
        with (
            patch.object(
                FF,
                "_probe_json",
                AsyncMock(side_effect=[{"streams": None}, {"streams": []}]),
            ),
            patch.object(FF, "_mediainfo_streams", AsyncMock(return_value=None)),
        ):
            result = await FF.probe_streams(self.path)

        self.assertEqual(result, {"video": [], "audio": [], "subtitle": []})

    async def test_probe_streams_has_a_hard_overall_deadline(self):
        async def never_finishes(_path):
            await asyncio.sleep(60)

        with (
            patch.object(FF, "PROBE_OVERALL_TIMEOUT_SECONDS", 0.01),
            patch.object(FF, "_probe_streams_uncached", never_finishes),
        ):
            with self.assertRaisesRegex(FF.MediaProbeError, "overall limit"):
                await FF.probe_streams(self.path)

    async def test_timed_out_ffprobe_process_is_killed_and_reaped(self):
        class FakeProcess:
            def __init__(self):
                self.calls = 0
                self.killed = False
                self.waited = False
                self.returncode = 0

            async def communicate(self):
                self.calls += 1
                if self.calls == 1:
                    await asyncio.sleep(60)
                return b"", b""

            def kill(self):
                self.killed = True

            async def wait(self):
                self.waited = True
                return 0

        proc = FakeProcess()
        with (
            patch.object(FF, "_PROBE_PROCESS_TIMEOUT_SECONDS", 0.01),
            patch.object(
                FF.asyncio,
                "create_subprocess_exec",
                AsyncMock(return_value=proc),
            ),
        ):
            with self.assertRaisesRegex(FF.MediaProbeError, "timed out"):
                await FF._probe_json(["-show_streams", self.path])

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertEqual(proc.calls, 1)

    async def test_missing_mediainfo_binary_falls_back_to_ffprobe(self):
        fallback = "General\nComplete name  : video.mkv"
        with (
            patch.object(
                FF.asyncio,
                "create_subprocess_exec",
                AsyncMock(side_effect=FileNotFoundError),
            ),
            patch.object(
                FF,
                "_ffprobe_mediainfo_text",
                AsyncMock(return_value=fallback),
            ) as ffprobe_fallback,
        ):
            result = await FF.get_mediainfo(self.path)

        self.assertEqual(result, fallback)
        ffprobe_fallback.assert_awaited_once_with(self.path)

    async def test_ffprobe_mediainfo_reuses_probe_data_for_duration(self):
        data = {
            "format": {"format_name": "matroska", "duration": "91.2"},
            "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}],
        }
        utils = types.ModuleType("services.utils")
        utils.human_size = lambda value: f"{value} bytes"
        utils.fmt_hms = lambda value: f"{int(value)}s"
        with (
            patch.dict(sys.modules, {"services.utils": utils}),
            patch.object(FF, "_probe_json", AsyncMock(return_value=data)),
            patch.object(
                FF,
                "probe_duration",
                AsyncMock(side_effect=AssertionError("unexpected second probe")),
            ),
        ):
            text = await FF._ffprobe_mediainfo_text(self.path)

        self.assertIn("Duration", text)
        self.assertIn("Video #0", text)

    def test_video_plugin_checks_size_and_delivers_probe_errors_importantly(self):
        src = Path("plugins/video.py").read_text(encoding="utf-8")
        self.assertIn("FF.verify_media_file(cached_path, session.fsize)", src)
        self.assertIn("FF.verify_media_file(path, session.fsize)", src)
        self.assertIn("async def _important_status_edit", src)
        self.assertIn('mode="normal"', src)
        self.assertIn("timeout=FF.PROBE_OVERALL_TIMEOUT_SECONDS + 1.0", src)


if __name__ == "__main__":
    unittest.main()
