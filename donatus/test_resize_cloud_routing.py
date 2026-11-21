import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("API_ID", "12345")
os.environ.setdefault("API_HASH", "test_hash")
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "1")

from plugins import resize  # noqa: E402
from services import cloudconvert_api, freeconvert_api  # noqa: E402


class ResizePanelTests(unittest.TestCase):
    def test_cloud_panel_shows_the_complete_workflow(self):
        panel = resize._cloud_workflow_panel(
            "FC RESIZE · 360p", "episode <15>.mkv", "upload",
            pct=25, done=25, total=100,
        )
        self.assertIn("WORKFLOW", panel)
        self.assertIn("Creating provider job", panel)
        self.assertIn("Uploading source video", panel)
        self.assertIn("Converting in the cloud", panel)
        self.assertIn("Downloading result", panel)
        self.assertIn("Delivering to Telegram", panel)
        self.assertIn("25%", panel)
        self.assertIn("episode &lt;15&gt;.mkv", panel)

    def test_compress_prompt_has_examples_and_provider_next_step(self):
        panel = resize._compress_prompt("episode.mkv", 100 * 1024 * 1024)
        self.assertIn("COMPRESS VIDEO", panel)
        self.assertIn("30", panel)
        self.assertIn("85", panel)
        self.assertIn("200", panel)
        self.assertIn("FreeConvert, CloudConvert, or Local", panel)


class _Response:
    status = 201

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return {"data": {"id": "job-1", "tasks": []}}


class _Session:
    last_payload = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, *, json, headers):
        type(self).last_payload = json
        return _Response(json)


class CloudConvertResizePayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_resize_uses_native_convert_task(self):
        with patch.object(cloudconvert_api.aiohttp, "ClientSession", _Session):
            await cloudconvert_api.create_convert_job(
                "key",
                video_filename="source.mkv",
                output_filename="resized.mp4",
                scale_height=720,
            )

        task = _Session.last_payload["tasks"]["convert"]
        self.assertEqual(task["operation"], "convert")
        self.assertEqual(task["input"], "import-video")
        self.assertEqual(task["output_format"], "mp4")
        self.assertEqual(task["height"], 720)
        self.assertNotIn("command", task)


class ResizeFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_freeconvert_failure_tries_cloudconvert_before_ffmpeg(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td, "source.mkv")
            result = Path(td, "source_720p.mp4")
            source.write_bytes(b"source")
            result.write_bytes(b"resized")

            msg = type("Message", (), {"delete": AsyncMock()})()
            client = type("Client", (), {"send_message": AsyncMock(return_value=object())})()

            fc_submit = AsyncMock(side_effect=RuntimeError("FC rejected job"))
            cc_submit = AsyncMock(return_value="cc-job")
            local_resize = AsyncMock(side_effect=AssertionError("local fallback was premature"))

            with (
                patch.object(resize, "_has_fc", return_value=True),
                patch.object(resize, "_has_cc", return_value=True),
                patch.object(resize, "safe_edit", new=AsyncMock()),
                patch.object(resize, "cleanup"),
                patch.object(freeconvert_api, "get_fc_api_keys", return_value=["fc-key"]),
                patch.object(freeconvert_api, "submit_with_fc_key_fallback", new=fc_submit),
                patch.object(cloudconvert_api, "submit_convert", new=cc_submit),
                patch.object(cloudconvert_api, "run_cc_job", new=AsyncMock(return_value=str(result))),
                patch("services.ffmpeg.resize_video", new=local_resize),
                patch("services.uploader.upload_file", new=AsyncMock()),
            ):
                await resize._do_resize(
                    client, msg, str(source), source.name, td, 720, 7,
                    platform="fc",
                )

            fc_submit.assert_awaited_once()
            cc_submit.assert_awaited_once()
            local_resize.assert_not_awaited()

    async def test_freeconvert_compress_failure_tries_cloudconvert_first(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td, "source.mkv")
            result = Path(td, "source_50MB.mp4")
            source.write_bytes(b"source")
            result.write_bytes(b"compressed")

            msg = type("Message", (), {"delete": AsyncMock()})()
            client = type("Client", (), {"send_message": AsyncMock(return_value=object())})()
            fc_submit = AsyncMock(side_effect=RuntimeError("FC upload failed"))
            cc_submit = AsyncMock(return_value="cc-compress-job")
            local_compress = AsyncMock(side_effect=AssertionError("local fallback was premature"))

            with (
                patch.object(resize, "_has_fc", return_value=True),
                patch.object(resize, "_has_cc", return_value=True),
                patch.object(resize, "safe_edit", new=AsyncMock()),
                patch.object(resize, "cleanup"),
                patch.object(freeconvert_api, "get_fc_api_keys", return_value=["fc-key"]),
                patch.object(freeconvert_api, "submit_with_fc_key_fallback", new=fc_submit),
                patch.object(cloudconvert_api, "submit_compress", new=cc_submit),
                patch.object(cloudconvert_api, "run_cc_job", new=AsyncMock(return_value=str(result))),
                patch("services.ffmpeg.probe_duration", new=AsyncMock(return_value=600)),
                patch("services.ffmpeg.compress_to_size", new=local_compress),
                patch("services.uploader.upload_file", new=AsyncMock()),
            ):
                await resize._do_compress(
                    client, msg, str(source), source.name, td, 50, 7,
                    platform="fc",
                )

            fc_submit.assert_awaited_once()
            cc_submit.assert_awaited_once()
            self.assertIn("upload_progress_cb", cc_submit.await_args.kwargs)
            local_compress.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
