"""Executable coverage for dual Telegram video preview assets."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageFilter

from services import smart_thumbnail


def _remove_paths(*paths: str | None) -> None:
    for path in paths:
        if path:
            Path(path).unlink(missing_ok=True)


class SmartThumbnailTests(unittest.TestCase):
    def test_asset_limits_match_telegram_roles(self):
        self.assertEqual(smart_thumbnail._COVER_MAX_SIDE, 1280)
        self.assertEqual(smart_thumbnail._THUMB_MAX_SIDE, 320)
        self.assertLessEqual(smart_thumbnail._MAX_SAFE_THUMB_BYTES, 200_000)
        self.assertEqual(smart_thumbnail._COVER_QUALITIES, (2, 3, 4))

    def test_midpoint_timestamp_is_inside_media(self):
        self.assertEqual(smart_thumbnail._midpoint_timestamp(100), 50)
        self.assertEqual(smart_thumbnail._midpoint_timestamp(3), 1)
        self.assertEqual(smart_thumbnail._midpoint_timestamp(0), 1)

    def test_sharp_proxy_scores_above_blurred_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sharp_path = Path(temp_dir, "sharp.png")
            blurred_path = Path(temp_dir, "blurred.png")
            image = Image.new("RGB", (256, 144), "white")
            pixels = image.load()
            for y in range(image.height):
                for x in range(image.width):
                    if (x // 8 + y // 8) % 2:
                        pixels[x, y] = (20, 40, 220)
            image.save(sharp_path)
            image.filter(ImageFilter.GaussianBlur(radius=4)).save(blurred_path)

            self.assertGreater(
                smart_thumbnail._score_candidate(str(sharp_path)),
                smart_thumbnail._score_candidate(str(blurred_path)),
            )


class SmartThumbnailAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_timed_out_ffmpeg_is_killed_and_reaped(self):
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
                return 0

        proc = HangingProcess()
        with mock.patch.object(smart_thumbnail, "_REAP_TIMEOUT_SECONDS", 0.01):
            self.assertFalse(await smart_thumbnail._finish_ffmpeg(proc, 0.01))

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_no_scored_candidate_uses_true_midpoint(self):
        calls: list[tuple[int, str]] = []

        async def fake_frame(_path, timestamp, output, **_kwargs):
            calls.append((timestamp, os.path.basename(output)))
            return False

        source = Path(self.id().replace(".", "_") + ".mkv")
        source.write_bytes(b"video")
        try:
            with mock.patch.object(
                smart_thumbnail,
                "_run_ffmpeg_frame",
                side_effect=fake_frame,
            ):
                timestamp = await smart_thumbnail._select_best_timestamp(
                    str(source),
                    duration=100,
                )

            self.assertEqual(timestamp, 50)
            self.assertIn((50, "fallback.jpg"), calls)
        finally:
            source.unlink(missing_ok=True)

    async def test_final_pair_uses_original_video_not_proxy(self):
        source = Path(self.id().replace(".", "_") + ".mkv")
        source.write_bytes(b"video")
        calls: list[tuple[str, int | None]] = []

        async def fake_select(_source, _duration):
            return 37

        async def fake_pair(pair_source, *, timestamp):
            calls.append((pair_source, timestamp))
            return "cover.jpg", "thumb.jpg"

        try:
            with (
                mock.patch.object(
                    smart_thumbnail,
                    "_select_best_timestamp",
                    side_effect=fake_select,
                ),
                mock.patch.object(
                    smart_thumbnail,
                    "_encode_preview_pair",
                    side_effect=fake_pair,
                ),
            ):
                result = await smart_thumbnail.make_smart_preview_assets(
                    str(source),
                    duration=100,
                )

            self.assertEqual(result, ("cover.jpg", "thumb.jpg"))
            self.assertEqual(calls, [(str(source), 37)])
        finally:
            source.unlink(missing_ok=True)

    async def test_cover_does_not_apply_legacy_byte_cap(self):
        output = Path(self.id().replace(".", "_") + ".jpg")

        async def fake_encode(_source, out_path, **kwargs):
            self.assertEqual(kwargs["max_side"], 1280)
            Path(out_path).write_bytes(b"x" * 250_000)
            return 250_000

        try:
            with mock.patch.object(
                smart_thumbnail,
                "_run_ffmpeg_encode",
                side_effect=fake_encode,
            ):
                self.assertTrue(
                    await smart_thumbnail._encode_cover(
                        "source.jpg",
                        str(output),
                        timestamp=None,
                    )
                )
        finally:
            output.unlink(missing_ok=True)

    async def test_failed_encoder_cancels_and_drains_sibling_before_cleanup(self):
        sibling_started = asyncio.Event()
        sibling_cancelled = False
        sibling_paths: list[str] = []

        async def failing_cover(*_args, **_kwargs):
            await sibling_started.wait()
            raise RuntimeError("cover encoder failed")

        async def late_thumb(_source, out_path, **_kwargs):
            nonlocal sibling_cancelled
            sibling_paths.append(out_path)
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled = True
                # Reproduce an encoder that finishes one last write while
                # handling cancellation. Cleanup must happen after this exits.
                await asyncio.sleep(0)
                Path(out_path).write_bytes(b"late thumbnail")
                return True

        try:
            with (
                mock.patch.object(
                    smart_thumbnail,
                    "_encode_cover",
                    side_effect=failing_cover,
                ),
                mock.patch.object(
                    smart_thumbnail,
                    "_encode_legacy_thumb",
                    side_effect=late_thumb,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "cover encoder failed"):
                    await smart_thumbnail._encode_preview_pair(
                        "source.mp4",
                        timestamp=10,
                    )

            self.assertTrue(sibling_cancelled)
            self.assertEqual(len(sibling_paths), 1)
            self.assertFalse(Path(sibling_paths[0]).exists())
            await asyncio.sleep(0.05)
            self.assertFalse(Path(sibling_paths[0]).exists())
        finally:
            for path in sibling_paths:
                Path(path).unlink(missing_ok=True)


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
class SmartThumbnailFFmpegTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_video(self, width: int, height: int, name: str) -> str:
        output = str(Path(self.temp_dir.name, name))
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={width}x{height}:rate=1",
                "-t",
                "3",
                "-c:v",
                "mpeg4",
                "-q:v",
                "4",
                output,
            ],
            check=True,
            timeout=30,
        )
        return output

    def assert_preview_pair(
        self,
        cover_path: str | None,
        thumb_path: str | None,
        *,
        cover_size: tuple[int, int],
        thumb_size: tuple[int, int],
    ) -> None:
        self.assertTrue(cover_path and os.path.isfile(cover_path))
        self.assertTrue(thumb_path and os.path.isfile(thumb_path))
        with Image.open(cover_path) as cover:
            self.assertEqual(cover.format, "JPEG")
            self.assertEqual(cover.size, cover_size)
            self.assertLessEqual(max(cover.size), 1280)
        with Image.open(thumb_path) as thumb:
            self.assertEqual(thumb.format, "JPEG")
            self.assertEqual(thumb.size, thumb_size)
            self.assertLessEqual(max(thumb.size), 320)
        self.assertLessEqual(
            os.path.getsize(thumb_path),
            smart_thumbnail._MAX_SAFE_THUMB_BYTES,
        )

    async def test_landscape_video_produces_1280_cover_and_320_thumb(self):
        source = self._make_video(1280, 720, "landscape.mp4")
        cover_path = thumb_path = None
        try:
            cover_path, thumb_path = await smart_thumbnail.make_smart_preview_assets(
                source,
                duration=3,
            )
            self.assert_preview_pair(
                cover_path,
                thumb_path,
                cover_size=(1280, 720),
                thumb_size=(320, 180),
            )
        finally:
            _remove_paths(cover_path, thumb_path)

    async def test_portrait_video_bounds_both_axes(self):
        source = self._make_video(720, 1280, "portrait.mp4")
        cover_path = thumb_path = None
        try:
            cover_path, thumb_path = await smart_thumbnail.make_smart_preview_assets(
                source,
                duration=3,
            )
            self.assert_preview_pair(
                cover_path,
                thumb_path,
                cover_size=(720, 1280),
                thumb_size=(180, 320),
            )
        finally:
            _remove_paths(cover_path, thumb_path)

    async def test_image_assets_do_not_upscale_cover(self):
        source = Path(self.temp_dir.name, "small.png")
        Image.new("RGB", (640, 360), (40, 120, 210)).save(source)
        cover_path = thumb_path = None
        try:
            cover_path, thumb_path = await smart_thumbnail.encode_preview_assets(str(source))
            self.assert_preview_pair(
                cover_path,
                thumb_path,
                cover_size=(640, 360),
                thumb_size=(320, 180),
            )
        finally:
            _remove_paths(cover_path, thumb_path)

    async def test_compatibility_thumbnail_is_legacy_sized(self):
        source = self._make_video(1280, 720, "compat.mp4")
        thumb_path = None
        try:
            thumb_path, owned = await smart_thumbnail.make_smart_thumbnail(
                source,
                duration=3,
            )
            self.assertTrue(owned)
            self.assertTrue(thumb_path)
            with Image.open(thumb_path) as thumb:
                self.assertEqual(thumb.size, (320, 180))
            self.assertLessEqual(
                os.path.getsize(thumb_path),
                smart_thumbnail._MAX_SAFE_THUMB_BYTES,
            )
        finally:
            _remove_paths(thumb_path)

    async def test_midpoint_compatibility_thumbnail_is_legacy_sized(self):
        source = self._make_video(720, 1280, "midpoint_compat.mp4")
        thumb_path = None
        try:
            thumb_path, owned = await smart_thumbnail.make_midpoint_thumbnail(
                source,
                duration=3,
            )
            self.assertTrue(owned)
            self.assertTrue(thumb_path)
            with Image.open(thumb_path) as thumb:
                self.assertEqual(thumb.size, (180, 320))
            self.assertLessEqual(
                os.path.getsize(thumb_path),
                smart_thumbnail._MAX_SAFE_THUMB_BYTES,
            )
        finally:
            _remove_paths(thumb_path)

    async def test_midpoint_preview_pair_keeps_the_hd_cover(self):
        """The never-black fallback must produce the HD cover too — the old
        thumb-only fallback silently downgraded videos to Telegram's soft
        auto-preview whenever the scored pass missed its deadline."""
        source = self._make_video(1280, 720, "midpoint_pair.mp4")
        cover_path = thumb_path = None
        try:
            cover_path, thumb_path = await smart_thumbnail.make_midpoint_preview_assets(
                source,
                duration=3,
            )
            self.assert_preview_pair(
                cover_path,
                thumb_path,
                cover_size=(1280, 720),
                thumb_size=(320, 180),
            )
        finally:
            _remove_paths(cover_path, thumb_path)

    async def test_encode_thumbnail_writes_requested_legacy_path(self):
        source = Path(self.temp_dir.name, "custom.png")
        output = Path(self.temp_dir.name, "custom_thumb.jpg")
        Image.new("RGB", (1280, 720), (210, 90, 30)).save(source)

        self.assertTrue(
            await smart_thumbnail.encode_thumbnail(str(source), str(output))
        )
        with Image.open(output) as thumb:
            self.assertEqual(thumb.format, "JPEG")
            self.assertEqual(thumb.size, (320, 180))
        self.assertLessEqual(
            output.stat().st_size,
            smart_thumbnail._MAX_SAFE_THUMB_BYTES,
        )


if __name__ == "__main__":
    unittest.main()
