from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services import subtitle_extract


class DirectSubtitleExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_one_probe_then_exact_global_stream_index(self):
        probe_data = {
            "subtitle": [
                {"index": 2, "codec_name": "aac", "tags": {"language": "fra"}},
                {
                    "index": 6,
                    "codec_name": "ass",
                    "tags": {"language": "fra"},
                    "disposition": {"default": 1},
                },
            ]
        }
        calls = []

        async def fake_stream_op(inp, out, args, timeout=0):
            calls.append((inp, out, args, timeout))
            Path(out).write_bytes(b"[Script Info]\n" + b"x" * 240)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                subtitle_extract.FF,
                "probe_streams_once",
                AsyncMock(return_value=probe_data),
            ) as probe,
            # No range support → extract_stream_smart falls back to the direct
            # URL scan, which is the classic single stream_op call.
            patch.object(
                subtitle_extract,
                "_remote_size_and_ranges",
                AsyncMock(return_value=(0, False)),
            ),
            patch.object(subtitle_extract.FF, "stream_op", side_effect=fake_stream_op),
        ):
            result = await subtitle_extract.extract_french_sub_direct(
                "https://seedr.example/video.mkv", tmp,
            )

        self.assertIsNotNone(result)
        probe.assert_awaited_once()
        self.assertEqual(len(calls), 1)
        self.assertIn("0:6", calls[0][2])
        self.assertEqual(calls[0][3], 55)
        self.assertIn("stream:6", result[1])

    async def test_no_french_text_stream_does_not_launch_ffmpeg(self):
        probe_data = {
            "subtitle": [
                {"index": 3, "codec_name": "ass", "tags": {"language": "eng"}},
                {"index": 4, "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "fra"}},
            ]
        }
        stream_op = AsyncMock()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(subtitle_extract.FF, "probe_streams_once", AsyncMock(return_value=probe_data)),
            patch.object(
                subtitle_extract,
                "_remote_size_and_ranges",
                AsyncMock(return_value=(0, False)),
            ),
            patch.object(subtitle_extract.FF, "stream_op", stream_op),
        ):
            result = await subtitle_extract.extract_french_sub_direct(
                "https://seedr.example/video.mkv", tmp,
            )

        self.assertIsNone(result)
        stream_op.assert_not_awaited()

    async def test_probe_timeout_returns_to_the_fallback_flow(self):
        stream_op = AsyncMock()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                subtitle_extract.FF,
                "probe_streams_once",
                AsyncMock(side_effect=subtitle_extract.FF.MediaProbeError("timed out")),
            ),
            patch.object(
                subtitle_extract,
                "_remote_size_and_ranges",
                AsyncMock(return_value=(0, False)),
            ),
            patch.object(subtitle_extract.FF, "stream_op", stream_op),
        ):
            result = await subtitle_extract.extract_french_sub_direct(
                "https://seedr.example/video.mkv", tmp,
            )

        self.assertIsNone(result)
        stream_op.assert_not_awaited()

    async def test_download_first_probes_and_extracts_locally(self):
        """The happy path never touches ffprobe/ffmpeg over HTTP: it pulls
        the file with parallel ranges, then probes + extracts the local copy."""
        size = 192 * 1024 * 1024
        probe_data = {
            "subtitle": [
                {"index": 2, "codec_name": "ass", "tags": {"language": "fra"}},
            ]
        }
        events = []

        async def fake_download(url, dest, dl_size, progress_cb=None):
            events.append(("download", url))
            Path(dest).write_bytes(b"video")

        async def fake_probe(target, timeout=None):
            events.append(("probe", target))
            return probe_data

        async def fake_stream_op(inp, out, args, timeout=0):
            events.append(("extract", inp))
            Path(out).write_bytes(b"[Script Info]\n" + b"x" * 240)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                subtitle_extract,
                "_remote_size_and_ranges",
                AsyncMock(return_value=(size, True)),
            ),
            patch.object(
                subtitle_extract, "_download_url_parallel", side_effect=fake_download
            ),
            patch.object(
                subtitle_extract.FF, "probe_streams_once", side_effect=fake_probe
            ),
            patch.object(subtitle_extract.FF, "stream_op", side_effect=fake_stream_op),
        ):
            result = await subtitle_extract.extract_french_sub_direct(
                "https://seedr.example/video.mkv", tmp,
            )
            leftovers = [
                p.name for p in Path(tmp).iterdir() if p.name.startswith(".subx_")
            ]

        self.assertIsNotNone(result)
        self.assertEqual([e[0] for e in events], ["download", "probe", "extract"])
        # Probe and extraction both ran on the LOCAL copy, never the URL.
        self.assertNotIn("://", events[1][1])
        self.assertNotIn("://", events[2][1])
        # The temp video was removed after the subtitle was produced.
        self.assertEqual(leftovers, [])
        self.assertIn("stream:2", result[1])


class ExtractStreamSmartTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_download_then_local_extract(self):
        size = 512 * 1024 * 1024  # ≥ _MIN_PARALLEL_BYTES
        calls = []

        async def fake_download(url, dest, dl_size, progress_cb=None):
            calls.append(("download", url, dest, dl_size, progress_cb))
            Path(dest).write_bytes(b"video")

        async def fake_stream_op(inp, out, args, timeout=0):
            calls.append(("extract", inp, out, args, timeout))
            Path(out).write_bytes(b"[Script Info]\n" + b"x" * 240)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                subtitle_extract,
                "_remote_size_and_ranges",
                AsyncMock(return_value=(size, True)),
            ),
            patch.object(
                subtitle_extract, "_download_url_parallel", side_effect=fake_download
            ),
            patch.object(subtitle_extract.FF, "stream_op", side_effect=fake_stream_op),
        ):
            async def progress(done, total):  # pragma: no cover - passthrough only
                pass

            out = str(Path(tmp) / "french_sub.ass")
            await subtitle_extract.extract_stream_smart(
                "https://seedr.example/video.mkv", out,
                ["-map", "0:2", "-c", "copy", "-vn", "-an"],
                size_hint=size, work_dir=tmp,
                progress_cb=progress,
            )
            # The temp video copy is removed after extraction.
            leftovers = [
                p.name for p in Path(tmp).iterdir() if p.name.startswith(".subx_")
            ]

        self.assertEqual([c[0] for c in calls], ["download", "extract"])
        # ffmpeg must read the downloaded LOCAL file, not the URL.
        extract_input = calls[1][1]
        self.assertNotIn("://", extract_input)
        self.assertTrue(extract_input.startswith(tmp))
        # The live-panel progress callback reaches the downloader.
        self.assertIs(calls[0][4], progress)
        self.assertEqual(leftovers, [])

    async def test_rate_limited_parallel_retries_with_sequential(self):
        """Seedr 429s parallel range bursts; the sequential single-connection
        download must take over and extraction still runs on the local copy."""
        size = 512 * 1024 * 1024
        events = []

        async def fake_sequential(url, dest, dl_size, progress_cb=None):
            events.append(("sequential", url))
            Path(dest).write_bytes(b"video")

        async def fake_stream_op(inp, out, args, timeout=0):
            events.append(("extract", inp))
            Path(out).write_bytes(b"sub")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                subtitle_extract,
                "_remote_size_and_ranges",
                AsyncMock(return_value=(size, True)),
            ),
            patch.object(
                subtitle_extract,
                "_download_url_parallel",
                AsyncMock(side_effect=RuntimeError("HTTP 429")),
            ),
            patch.object(
                subtitle_extract, "_download_url_sequential",
                side_effect=fake_sequential,
            ),
            patch.object(subtitle_extract.FF, "stream_op", side_effect=fake_stream_op),
        ):
            out = str(Path(tmp) / "french_sub.ass")
            await subtitle_extract.extract_stream_smart(
                "https://seedr.example/video.mkv", out,
                ["-map", "0:2", "-c", "copy", "-vn", "-an"],
                size_hint=size, work_dir=tmp,
            )

        self.assertEqual([e[0] for e in events], ["sequential", "extract"])
        # Extraction ran on the local copy, never on the URL.
        self.assertNotIn("://", events[1][1])

    async def test_both_downloads_failing_falls_back_to_url_scan(self):
        size = 512 * 1024 * 1024
        scans = []

        async def fake_stream_op(inp, out, args, timeout=0):
            scans.append((inp, timeout))
            Path(out).write_bytes(b"sub")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                subtitle_extract,
                "_remote_size_and_ranges",
                AsyncMock(return_value=(size, True)),
            ),
            patch.object(
                subtitle_extract,
                "_download_url_parallel",
                AsyncMock(side_effect=RuntimeError("segment incomplete")),
            ),
            patch.object(
                subtitle_extract,
                "_download_url_sequential",
                AsyncMock(side_effect=RuntimeError("HTTP 429")),
            ),
            patch.object(subtitle_extract.FF, "stream_op", side_effect=fake_stream_op),
        ):
            out = str(Path(tmp) / "french_sub.ass")
            await subtitle_extract.extract_stream_smart(
                "https://seedr.example/video.mkv", out,
                ["-map", "0:2", "-c", "copy", "-vn", "-an"],
                size_hint=size, work_dir=tmp,
            )

        self.assertEqual(len(scans), 1)
        self.assertIn("://", scans[0][0])
        # Size-scaled fallback timeout: 512 MB → 512 s, bounded to 600.
        self.assertEqual(scans[0][1], 512)

    async def test_small_files_use_sequential_not_parallel(self):
        """Below the parallel threshold the file is still brought local with
        one sequential connection — never handed to ffmpeg over HTTP."""
        events = []

        async def fake_sequential(url, dest, dl_size, progress_cb=None):
            events.append(("sequential", url))
            Path(dest).write_bytes(b"video")

        async def fake_stream_op(inp, out, args, timeout=0):
            events.append(("extract", inp))
            Path(out).write_bytes(b"sub")

        parallel = AsyncMock()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                subtitle_extract,
                "_remote_size_and_ranges",
                AsyncMock(return_value=(10 * 1024 * 1024, True)),
            ),
            patch.object(subtitle_extract, "_download_url_parallel", parallel),
            patch.object(
                subtitle_extract, "_download_url_sequential",
                side_effect=fake_sequential,
            ),
            patch.object(subtitle_extract.FF, "stream_op", side_effect=fake_stream_op),
        ):
            out = str(Path(tmp) / "french_sub.ass")
            await subtitle_extract.extract_stream_smart(
                "https://seedr.example/video.mkv", out,
                ["-map", "0:2", "-c", "copy"],
                work_dir=tmp,
            )

        parallel.assert_not_awaited()
        self.assertEqual([e[0] for e in events], ["sequential", "extract"])
        self.assertNotIn("://", events[1][1])

    async def test_local_source_extracts_directly(self):
        calls = []

        async def fake_stream_op(inp, out, args, timeout=0):
            calls.append(inp)
            Path(out).write_bytes(b"sub")

        ranges = AsyncMock()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(subtitle_extract, "_remote_size_and_ranges", ranges),
            patch.object(subtitle_extract.FF, "stream_op", side_effect=fake_stream_op),
        ):
            src = Path(tmp) / "video.mkv"
            src.write_bytes(b"mkv")
            out = str(Path(tmp) / "french_sub.ass")
            await subtitle_extract.extract_stream_smart(
                str(src), out, ["-map", "0:2", "-c", "copy"],
            )

        ranges.assert_not_awaited()
        self.assertEqual(calls, [str(src)])


class ConnectionScalingTests(unittest.TestCase):
    """Large videos get more range connections so the subtitle stage does not
    crawl; small files stay polite to avoid Seedr 429s."""

    def test_connections_scale_with_size(self):
        gib = 1024 ** 3
        with patch.dict(os.environ, {"SUB_EXTRACT_CONNECTIONS": ""}):
            self.assertEqual(subtitle_extract._connections_for_size(200 * 1024 * 1024), 4)
            self.assertEqual(subtitle_extract._connections_for_size(2 * gib), 6)
            self.assertEqual(subtitle_extract._connections_for_size(6 * gib), 8)

    def test_explicit_env_override_always_wins(self):
        with patch.dict(os.environ, {"SUB_EXTRACT_CONNECTIONS": "3"}):
            self.assertEqual(subtitle_extract._connections_for_size(6 * 1024 ** 3), 3)


if __name__ == "__main__":
    unittest.main()
