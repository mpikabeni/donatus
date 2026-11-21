"""Focused regression tests for upload thumbnails and oversized splitting.

The uploader normally imports Pyrogram and the configured bot singleton. These
tests load it with tiny dependency stubs so the cache/split helpers remain
executable in lightweight source-assertion test environments too.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


UPLOADER_PATH = Path("services/uploader.py")
UPLOADER_SOURCE = UPLOADER_PATH.read_text(encoding="utf-8")


def _module(name: str, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _load_uploader():
    class FloodWait(Exception):
        def __init__(self, value=1):
            super().__init__(value)
            self.value = value

    class DummySettings:
        async def get(self, _user_id):
            return {}

    class DummyLimiter:
        max_concurrent = 3

        def will_queue(self, _chat_id):
            return False

        def slot(self, _chat_id):
            class Slot:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return False

            return Slot()

    class DummyPanelUpdater:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def tick(self, **_kwargs):
            pass

    enums = types.SimpleNamespace(
        ChatType=types.SimpleNamespace(PRIVATE="private"),
        ParseMode=types.SimpleNamespace(HTML="html"),
    )

    async def _async_none(*_args, **_kwargs):
        return None

    stubs = {
        "pyrogram": _module("pyrogram", Client=object, enums=enums),
        "pyrogram.errors": _module("pyrogram.errors", FloodWait=FloodWait),
        "core.config": _module("core.config", cfg=types.SimpleNamespace(log_channel=0)),
        "core.session": _module("core.session", settings=DummySettings()),
        "services.background": _module("services.background", spawn=lambda _coro: None),
        "services.utils": _module(
            "services.utils",
            PanelUpdater=DummyPanelUpdater,
            human_size=lambda size: str(size),
            progress_panel=lambda **_kwargs: "panel",
            progress_update_policy=lambda *_args, **_kwargs: {},
            safe_edit=_async_none,
        ),
        "services.telegram_rate_limiter": _module(
            "services.telegram_rate_limiter",
            clear_edit_throttle=lambda _msg: False,
            safe_delete_message=_async_none,
            safe_edit_message=_async_none,
            safe_send_message=_async_none,
        ),
        "services.upload_limiter": _module(
            "services.upload_limiter",
            chat_upload_limiter=DummyLimiter(),
        ),
        "plugins.caption_templates": _module(
            "plugins.caption_templates",
            build_caption=_async_none,
            has_custom_template=lambda _channel_id: False,
        ),
    }

    module_name = f"_uploader_test_{id(stubs)}"
    spec = importlib.util.spec_from_file_location(module_name, UPLOADER_PATH)
    assert spec and spec.loader
    uploader = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, stubs):
        spec.loader.exec_module(uploader)
    return uploader


class ThumbnailCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uploader = _load_uploader()
        self.uploader._CUSTOM_THUMB_CACHE.clear()
        self.uploader._CUSTOM_THUMB_INFLIGHT.clear()

    async def test_concurrent_cache_miss_downloads_file_id_once(self):
        jpeg = b"\xff\xd8" + (b"x" * 3_000) + b"\xff\xd9"
        calls = 0

        async def fake_download(_client, file_id):
            nonlocal calls
            calls += 1
            self.assertEqual(file_id, "saved-file-id")
            await asyncio.sleep(0)
            return self.uploader._PreviewBytes(cover=jpeg, thumb=jpeg)

        with mock.patch.object(
            self.uploader,
            "_download_custom_preview_bytes",
            side_effect=fake_download,
        ):
            first, second = await asyncio.gather(
                self.uploader._custom_preview_bytes(object(), "saved-file-id"),
                self.uploader._custom_preview_bytes(object(), "saved-file-id"),
            )
            third = await self.uploader._custom_preview_bytes(object(), "saved-file-id")

        self.assertEqual(calls, 1)
        self.assertEqual(first, self.uploader._PreviewBytes(jpeg, jpeg))
        self.assertEqual(second, first)
        self.assertEqual(third, first)

        path_one = await self.uploader._materialize_custom_preview(
            object(), "saved-file-id"
        )
        path_two = await self.uploader._materialize_custom_preview(
            object(), "saved-file-id"
        )
        try:
            self.assertNotEqual(path_one, path_two)
            for path in (*path_one, *path_two):
                self.assertEqual(Path(path).read_bytes(), jpeg)
        finally:
            self.uploader._remove_owned_previews((*path_one, *path_two))

    async def test_saved_thumbnail_has_priority_and_auto_is_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom_cover = os.path.join(tmp, "custom_cover.jpg")
            custom_thumb = os.path.join(tmp, "custom_thumb.jpg")
            auto_cover = os.path.join(tmp, "auto_cover.jpg")
            auto_thumb = os.path.join(tmp, "auto_thumb.jpg")
            for path in (custom_cover, custom_thumb, auto_cover, auto_thumb):
                Path(path).write_bytes(b"preview")

            with (
                mock.patch.object(
                    self.uploader,
                    "_materialize_custom_preview",
                    new=mock.AsyncMock(return_value=(custom_cover, custom_thumb)),
                ),
                mock.patch.object(
                    self.uploader,
                    "_make_preview",
                    new=mock.AsyncMock(return_value=(auto_cover, auto_thumb)),
                ) as make_preview,
            ):
                thumb, cover, owned = await self.uploader._prepare_video_preview(
                    object(),
                    "episode.mkv",
                    1_200,
                    {"thumb_id": "saved-file-id"},
                    explicit_thumb="caller.jpg",
                )

            self.assertEqual((thumb, cover), (custom_thumb, custom_cover))
            self.assertEqual(set(owned), {custom_cover, custom_thumb})
            make_preview.assert_not_awaited()

            with (
                mock.patch.object(
                    self.uploader,
                    "_materialize_custom_preview",
                    new=mock.AsyncMock(return_value=None),
                ),
                mock.patch.object(
                    self.uploader,
                    "_make_preview",
                    new=mock.AsyncMock(return_value=(auto_cover, auto_thumb)),
                ),
            ):
                thumb, cover, owned = await self.uploader._prepare_video_preview(
                    object(),
                    "episode.mkv",
                    1_200,
                    {"thumb_id": "stale-file-id"},
                )

            self.assertEqual((thumb, cover), (auto_thumb, auto_cover))
            self.assertEqual(set(owned), {auto_cover, auto_thumb})

    async def test_no_preview_does_not_discard_completed_video(self):
        with mock.patch.object(
            self.uploader,
            "_make_preview",
            new=mock.AsyncMock(return_value=(None, None)),
        ):
            result = await self.uploader._prepare_video_preview(
                object(), "episode.mkv", 1_200, {}
            )

        self.assertEqual(result, (None, None, ()))

    async def test_large_saved_jpeg_fallback_is_cover_only(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            image_path = os.path.join(tmp, "saved.jpg")
            Image.new("RGB", (1280, 720), "navy").save(
                image_path, format="JPEG", quality=70
            )

            self.assertIsNotNone(
                self.uploader._read_jpeg(
                    image_path,
                    max_bytes=self.uploader._MAX_SAFE_COVER_BYTES,
                )
            )
            self.assertIsNone(self.uploader._read_safe_jpeg(image_path))

    async def test_force_document_generates_only_legacy_thumbnail(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = os.path.join(tmp, "episode.mkv")
            thumb = os.path.join(tmp, "thumb.jpg")
            Path(video).write_bytes(b"video")
            Path(thumb).write_bytes(b"thumb")
            with (
                mock.patch.object(
                    self.uploader,
                    "_make_legacy_preview",
                    new=mock.AsyncMock(return_value=thumb),
                ) as legacy,
                mock.patch.object(
                    self.uploader,
                    "_make_preview",
                    new=mock.AsyncMock(),
                ) as dual,
            ):
                result = await self.uploader._prepare_video_preview(
                    object(),
                    video,
                    1_200,
                    {},
                    include_cover=False,
                )

        self.assertEqual(result, (thumb, None, (thumb,)))
        legacy.assert_awaited_once_with(video, 1_200)
        dual.assert_not_awaited()


class SplitHelperTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uploader = _load_uploader()

    async def test_binary_split_is_bounded_low_memory_and_reconstructable(self):
        payload = bytes(range(25))
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "episode.mkv")
            output_dir = os.path.join(tmp, "parts")
            os.mkdir(output_dir)
            Path(source).write_bytes(payload)

            parts = await self.uploader._split_binary(source, output_dir, 10)

            self.assertEqual([os.path.getsize(part) for part in parts], [10, 10, 5])
            self.assertTrue(parts[0].endswith("episode.mkv.part001"))
            self.assertEqual(b"".join(Path(part).read_bytes() for part in parts), payload)

    async def test_split_validation_rejects_oversized_or_incomplete_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "part1")
            second = os.path.join(tmp, "part2")
            Path(first).write_bytes(b"a" * 50)
            Path(second).write_bytes(b"b" * 50)

            self.assertTrue(
                self.uploader._valid_split_parts(
                    [first, second], original_size=100, max_part_bytes=60
                )
            )
            self.assertFalse(
                self.uploader._valid_split_parts(
                    [first, second], original_size=100, max_part_bytes=49
                )
            )
            self.assertFalse(
                self.uploader._valid_split_parts(
                    [first], original_size=100, max_part_bytes=60
                )
            )

    async def test_split_post_hooks_finish_before_temp_files_are_deleted(self):
        tmp = tempfile.mkdtemp()
        part = os.path.join(tmp, "episode.part001")
        Path(part).write_bytes(b"part")
        observed = []

        async def fake_hook(_sent, path, _user_id):
            await asyncio.sleep(0)
            observed.append(os.path.isfile(path))

        with mock.patch.object(
            self.uploader,
            "_run_post_upload_hooks",
            side_effect=fake_hook,
        ):
            await self.uploader._run_split_post_hooks_and_cleanup(
                [(object(), part, 123)],
                tmp,
            )

        self.assertEqual(observed, [True])
        self.assertFalse(os.path.exists(tmp))


class UploadLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uploader = _load_uploader()

    async def test_success_clears_throttle_then_collapses_when_delete_fails(self):
        clear = mock.Mock(return_value=True)
        delete = mock.AsyncMock(return_value=False)
        edit = mock.AsyncMock(return_value=True)
        spawn = mock.Mock()

        with (
            mock.patch.object(self.uploader, "clear_edit_throttle", clear),
            mock.patch.object(self.uploader, "safe_delete_message", delete),
            mock.patch.object(self.uploader, "safe_edit_message", edit),
            mock.patch.object(self.uploader, "spawn", spawn),
        ):
            await self.uploader._delete_or_mark_complete(object(), "A&B.mkv")

        self.assertGreaterEqual(clear.call_count, 2)
        delete.assert_awaited_once()
        self.assertIn("A&amp;B.mkv", edit.await_args.args[1])
        spawn.assert_not_called()

    async def test_failure_clears_throttle_and_escapes_telegram_html(self):
        clear = mock.Mock(return_value=True)
        edit = mock.AsyncMock(return_value=True)
        with (
            mock.patch.object(self.uploader, "clear_edit_throttle", clear),
            mock.patch.object(self.uploader, "safe_edit_message", edit),
        ):
            await self.uploader._mark_upload_failed(
                object(), "bad<name>.mkv", RuntimeError("codec <broken>")
            )

        clear.assert_called_once()
        text = edit.await_args.args[1]
        self.assertIn("bad&lt;name&gt;.mkv", text)
        self.assertIn("codec &lt;broken&gt;", text)

    def test_video_metadata_ignores_cover_art_and_applies_rotation(self):
        meta = self.uploader._parse_video_meta({
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1200,
                    "height": 1200,
                    "disposition": {"attached_pic": 1},
                },
                {
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "duration": "90.4",
                    "side_data_list": [{"rotation": -90}],
                    "disposition": {"default": 1},
                },
            ],
            "format": {"duration": "91"},
        })

        self.assertEqual(meta, {"duration": 90, "width": 1080, "height": 1920})

    async def test_preview_failure_never_discards_completed_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = os.path.join(tmp, "episode.mp4")
            Path(video).write_bytes(b"video")
            ready = mock.AsyncMock()
            with (
                mock.patch.object(
                    self.uploader,
                    "_video_meta",
                    new=mock.AsyncMock(
                        return_value={"duration": 10, "width": 1280, "height": 720}
                    ),
                ),
                mock.patch.object(
                    self.uploader,
                    "_prepare_video_preview",
                    new=mock.AsyncMock(return_value=(None, None, ())),
                ),
                mock.patch.object(self.uploader, "_upload_single_ready", ready),
            ):
                await self.uploader._upload_single(object(), object(), video)

        ready.assert_awaited_once()
        self.assertIsNone(ready.await_args.kwargs["thumb"])
        self.assertIsNone(ready.await_args.kwargs["cover"])

    async def test_separate_cover_and_thumb_reach_video_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = os.path.join(tmp, "episode.mp4")
            cover = os.path.join(tmp, "cover.jpg")
            thumb = os.path.join(tmp, "thumb.jpg")
            Path(video).write_bytes(b"video")
            Path(cover).write_bytes(b"cover")
            Path(thumb).write_bytes(b"thumb")
            ready = mock.AsyncMock()
            with (
                mock.patch.object(
                    self.uploader,
                    "_video_meta",
                    new=mock.AsyncMock(
                        return_value={"duration": 10, "width": 1280, "height": 720}
                    ),
                ),
                mock.patch.object(
                    self.uploader,
                    "_prepare_video_preview",
                    new=mock.AsyncMock(return_value=(thumb, cover, ())),
                ),
                mock.patch.object(self.uploader, "_upload_single_ready", ready),
            ):
                await self.uploader._upload_single(object(), object(), video)

        ready.assert_awaited_once()
        self.assertEqual(ready.await_args.kwargs["thumb"], thumb)
        self.assertEqual(ready.await_args.kwargs["cover"], cover)

    def test_preview_rpc_errors_trigger_coverless_retry(self):
        self.assertTrue(
            self.uploader._is_preview_upload_error(
                RuntimeError("400 PHOTO_INVALID_DIMENSIONS")
            )
        )
        self.assertTrue(
            self.uploader._is_preview_upload_error(
                RuntimeError("VIDEO_COVER_THUMBNAIL_MISSING")
            )
        )
        self.assertFalse(
            self.uploader._is_preview_upload_error(RuntimeError("NETWORK_TIMEOUT"))
        )

    async def test_video_sender_retries_without_rejected_custom_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = os.path.join(tmp, "episode.mp4")
            Path(video).write_bytes(b"video payload")
            sent = object()
            client = types.SimpleNamespace(
                send_video=mock.AsyncMock(
                    side_effect=[
                        RuntimeError("400 PHOTO_INVALID_DIMENSIONS"),
                        sent,
                    ]
                )
            )
            msg = types.SimpleNamespace(chat=types.SimpleNamespace(id=99))
            task_runner_stub = _module(
                "services.task_runner",
                _stats_cache={"cpu": 0, "ram_used": 0, "disk_free": 0},
            )
            with (
                mock.patch.dict(sys.modules, {"services.task_runner": task_runner_stub}),
                mock.patch.object(
                    self.uploader,
                    "_delete_or_mark_complete",
                    new=mock.AsyncMock(),
                ),
            ):
                result = await self.uploader._upload_single_ready(
                    client,
                    msg,
                    video,
                    caption="",
                    thumb="thumb.jpg",
                    cover="cover.jpg",
                    force_document=False,
                    user_id=123,
                    user_cfg={},
                    ftype="video",
                    meta={"duration": 10, "width": 1280, "height": 720},
                    target_chat_id=None,
                    run_post_hooks=False,
                )

        self.assertIs(result, sent)
        self.assertEqual(client.send_video.await_count, 2)
        first = client.send_video.await_args_list[0].kwargs
        second = client.send_video.await_args_list[1].kwargs
        self.assertEqual((first["thumb"], first["cover"]), ("thumb.jpg", "cover.jpg"))
        self.assertEqual((second["thumb"], second["cover"]), (None, None))


class UploadWiringTests(unittest.TestCase):
    def test_video_send_has_thumb_streaming_and_real_metadata_kwargs(self):
        self.assertIn("thumb      = thumb", UPLOADER_SOURCE)
        self.assertIn("cover              = cover", UPLOADER_SOURCE)
        self.assertIn("supports_streaming = True", UPLOADER_SOURCE)
        self.assertIn('width              = meta["width"]', UPLOADER_SOURCE)
        self.assertIn('height             = meta["height"]', UPLOADER_SOURCE)
        self.assertIn('duration           = meta["duration"]', UPLOADER_SOURCE)

    def test_saved_thumbnail_is_downloaded_and_cleaned(self):
        self.assertIn("client.download_media(file_id", UPLOADER_SOURCE)
        self.assertIn('thumb_id = user_cfg.get("thumb_id")', UPLOADER_SOURCE)
        self.assertIn(
            "finally:\n        _remove_owned_previews(owned_previews)",
            UPLOADER_SOURCE,
        )

    def test_pyrofork_pin_supports_video_cover(self):
        requirements = Path("requirements.txt").read_text(encoding="utf-8")
        self.assertIn("pyrofork==2.3.69", requirements)

    def test_split_has_headroom_and_partial_failure_summary(self):
        uploader = _load_uploader()
        self.assertLess(uploader.TG_MAX_BYTES, 2 * 1024 * 1024 * 1024)
        self.assertGreaterEqual(
            uploader.TG_MAX_BYTES - uploader._SPLIT_TARGET_BYTES,
            64 * 1024 * 1024,
        )
        self.assertIn("failed_parts.append((i, str(exc)))", UPLOADER_SOURCE)
        self.assertIn("All {total_parts} parts uploaded", UPLOADER_SOURCE)
        self.assertIn("Failed parts:", UPLOADER_SOURCE)


if __name__ == "__main__":
    unittest.main()
