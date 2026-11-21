from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from services import ffmpeg as FF
from services import ffmpeg_runner
from services import video_presets
from services.video_pipeline import ProcessingStage, VideoJob, VideoPipeline


class VideoPresetTests(unittest.TestCase):
    def test_required_presets_exist_and_are_telegram_aware(self):
        for name in (
            "fast",
            "balanced",
            "small",
            "best",
            "telegram_safe",
            "anime_hardsub",
            "remux_copy",
        ):
            self.assertIn(name, video_presets.PRESETS)
        self.assertTrue(video_presets.get_preset("telegram_safe").telegram_compatible)
        self.assertEqual(video_presets.get_preset("remux_copy").video_codec, "copy")


class FFmpegRunnerTests(unittest.TestCase):
    def test_progress_parser_supports_progress_pipe(self):
        state = {}
        state.update(ffmpeg_runner.parse_ffmpeg_progress_line("out_time_ms=5000000", 10))
        state.update(ffmpeg_runner.parse_ffmpeg_progress_line("speed=2.0x", 10))
        progress = ffmpeg_runner.build_progress(state, 10)
        self.assertAlmostEqual(progress.current_time, 5.0)
        self.assertAlmostEqual(progress.percent, 50.0)
        self.assertEqual(progress.speed, 2.0)
        self.assertEqual(progress.eta, 2)

    def test_progress_parser_supports_stderr_status(self):
        update = ffmpeg_runner.parse_ffmpeg_progress_line(
            "frame=  48 fps=24.0 q=-1.0 size=1024kB time=00:00:02.00 bitrate=4194.3kbits/s speed=1.5x",
            4,
        )
        self.assertEqual(update["frame"], 48)
        self.assertEqual(update["fps"], 24.0)
        self.assertEqual(update["current_time"], 2.0)
        self.assertEqual(update["speed"], 1.5)


class FFmpegSmartHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_resize_command_generation_uses_no_upscale_even_scale(self):
        captured = {}
        old_run = ffmpeg_runner.run_ffmpeg
        old_probe = FF.probe_duration

        async def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

        async def fake_probe(path):
            return 60

        ffmpeg_runner.run_ffmpeg = fake_run
        FF.probe_duration = fake_probe
        try:
            await FF.resize_video_smart("input.mkv", "out.mp4", 720, crf=24)
        finally:
            ffmpeg_runner.run_ffmpeg = old_run
            FF.probe_duration = old_probe

        cmd = captured["cmd"]
        self.assertIn("-vf", cmd)
        self.assertIn("scale=w=-2:h=min(720\\,ih)", cmd)
        self.assertIn("-pix_fmt", cmd)
        self.assertIn("yuv420p", cmd)
        self.assertEqual(captured["kwargs"]["duration"], 60)

    async def test_validate_media_uses_ffprobe_contract(self):
        old_probe_full = FF._probe_full
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as fh:
            fh.write(b"x" * 10)
            path = fh.name

        async def fake_probe_full(_path):
            return {
                "format": {"duration": "12.5"},
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "pix_fmt": "yuv420p",
                        "width": 640,
                        "height": 360,
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
            }

        FF._probe_full = fake_probe_full
        try:
            result = await FF.validate_media(path, telegram_playable=True)
        finally:
            FF._probe_full = old_probe_full
            os.unlink(path)

        self.assertTrue(result["ok"])
        self.assertTrue(result["telegram_playable"])
        self.assertEqual(result["duration"], 12.5)

    def test_hardsub_subtitle_path_escaping(self):
        filt = FF._subtitle_filter(r"C:\tmp\a:b'sub.ass")
        self.assertTrue(filt.startswith("ass='"))
        self.assertIn("C\\:", filt)
        self.assertIn("a\\:b", filt)
        self.assertIn("\\'sub.ass", filt)

    def test_compress_target_bitrate_calculation(self):
        self.assertEqual(
            FF.estimate_target_video_bitrate_kbps(100, duration_s=100, audio_kbps=128),
            8064,
        )

    def test_telegram_split_decision(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"x" * 101)
            path = fh.name
        try:
            result = FF.telegram_split_decision(path, limit_bytes=100)
        finally:
            os.unlink(path)
        self.assertTrue(result["needs_split"])
        self.assertEqual(result["parts"], 2)


class VideoPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_transitions_for_successful_pipeline(self):
        updates = []

        class Record:
            tid = "PIPE1"
            mode = "proc"
            label = "pipeline"

            def update(self, **kwargs):
                updates.append(kwargs)

        job = VideoJob(user_id=1, source_type="local", source="input.mp4", action="probe")
        pipeline = VideoPipeline(job, task_record=Record(), cleanup=False)

        async def prepare():
            await pipeline._set_stage(ProcessingStage.DOWNLOADING, "Preparing")
            pipeline.job.input_path = "input.mp4"
            return "input.mp4"

        async def probe():
            await pipeline._set_stage(ProcessingStage.PROBING, "Probing")
            pipeline.result.streams = {"video": [{}], "audio": [], "subtitle": []}
            pipeline.result.duration = 1
            return {}

        pipeline.prepare_input = prepare
        pipeline.probe_input = probe
        result = await pipeline.run()

        self.assertTrue(result.ok)
        self.assertEqual(pipeline.stage, ProcessingStage.DONE)
        self.assertIn("Done", [u.get("stage") for u in updates])

    async def test_cancellation_sets_cancelled_stage(self):
        job = VideoJob(user_id=1, source_type="local", source="input.mp4", action="convert")
        pipeline = VideoPipeline(job, cleanup=False)

        async def prepare():
            pipeline.job.input_path = "input.mp4"
            return "input.mp4"

        async def probe():
            return {}

        async def process():
            raise asyncio.CancelledError

        pipeline.prepare_input = prepare
        pipeline.probe_input = probe
        pipeline.process = process

        with self.assertRaises(asyncio.CancelledError):
            await pipeline.run()
        self.assertEqual(pipeline.stage, ProcessingStage.CANCELLED)

    async def test_smart_download_can_use_existing_task_record_without_duplicate(self):
        import sys
        import types

        if "aiohttp" not in sys.modules:
            aiohttp_stub = types.ModuleType("aiohttp")

            class ClientTimeout:
                def __init__(self, *args, **kwargs):
                    pass

            class TCPConnector:
                def __init__(self, *args, **kwargs):
                    self.closed = False

                async def close(self):
                    self.closed = True

            class ClientSession:
                def __init__(self, *args, **kwargs):
                    pass

            aiohttp_stub.ClientTimeout = ClientTimeout
            aiohttp_stub.TCPConnector = TCPConnector
            aiohttp_stub.ClientSession = ClientSession
            sys.modules["aiohttp"] = aiohttp_stub

        if "yt_dlp" not in sys.modules:
            ytdlp_stub = types.ModuleType("yt_dlp")
            ytdlp_stub.YoutubeDL = object
            sys.modules["yt_dlp"] = ytdlp_stub

        if "dotenv" not in sys.modules:
            dotenv_stub = types.ModuleType("dotenv")
            dotenv_stub.load_dotenv = lambda *args, **kwargs: None
            sys.modules["dotenv"] = dotenv_stub
        if "pyrogram" not in sys.modules:
            pyrogram_stub = types.ModuleType("pyrogram")

            class Client:
                pass

            pyrogram_stub.Client = Client
            sys.modules["pyrogram"] = pyrogram_stub
        os.environ.setdefault("API_ID", "1")
        os.environ.setdefault("API_HASH", "test")
        os.environ.setdefault("BOT_TOKEN", "123:test")
        os.environ.setdefault("OWNER_ID", "1")

        from services import downloader
        from services.task_runner import TaskRecord, tracker

        tracker._tasks.clear()
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "video.mp4")
        record = TaskRecord(tid="PIPEJOB1", user_id=1, label="one")
        await tracker.register(record)
        old_dispatch = downloader._dispatch

        async def fake_dispatch(*args, **kwargs):
            with open(path, "wb") as fh:
                fh.write(b"x")
            return path

        downloader._dispatch = fake_dispatch
        try:
            result = await downloader.smart_download(
                "https://example.invalid/video.mp4",
                tmp,
                user_id=1,
                task_record=record,
            )
            task_count = len(tracker._tasks)
        finally:
            downloader._dispatch = old_dispatch
            tracker._tasks.clear()
            try:
                os.unlink(path)
                os.rmdir(tmp)
            except OSError:
                pass

        self.assertEqual(result, path)
        self.assertEqual(task_count, 1)


class URLHandlerSourceTests(unittest.TestCase):
    def test_url_handler_routes_processing_to_pipeline(self):
        src = Path("plugins/url_handler.py").read_text(encoding="utf-8")
        self.assertIn("submit_video_pipeline", src)
        self.assertIn("_submit_url_pipeline", src)
        self.assertIn('action="sample"', src)
        self.assertIn('action="compress"', src)
        self.assertIn("task_record=record", src)


if __name__ == "__main__":
    unittest.main()
