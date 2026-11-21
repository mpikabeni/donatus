"""Regression tests for multi-plugin/service audit fixes."""
from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("API_ID", "12345")
os.environ.setdefault("API_HASH", "test_hash")
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests._pyrogram_stub import install as _install_pyrogram_stub

_install_pyrogram_stub()

try:
    import aiohttp  # noqa: F401
except ImportError:
    aiohttp_stub = types.ModuleType("aiohttp")

    class _ClientTimeout:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _ClientSession:
        pass

    aiohttp_stub.ClientTimeout = _ClientTimeout
    aiohttp_stub.ClientSession = _ClientSession
    sys.modules["aiohttp"] = aiohttp_stub


def _message_handler_group(relative_path: str, function_name: str) -> int:
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == function_name:
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name != "on_message":
                    continue
                for kw in dec.keywords:
                    if kw.arg == "group":
                        return int(ast.literal_eval(kw.value))
                return 0
    raise AssertionError(f"Handler {function_name!r} not found in {relative_path}")


class SubtitleCacheAuditTests(unittest.TestCase):
    def test_entry_from_raw_and_get_cached_subtitle_ignore_unknown_keys(self):
        from services import subtitle_cache

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            cache_dir.mkdir()
            sub_file = cache_dir / "ep1.ass"
            sub_file.write_text("[Script Info]\nTitle: test\n", encoding="utf-8")
            index_path = cache_dir / "index.json"
            eid = subtitle_cache._entry_id("crc:DEADBEEF", "fr")
            index_path.write_text(
                json.dumps({
                    eid: {
                        "key": "crc:DEADBEEF",
                        "lang": "fr",
                        "path": str(sub_file),
                        "size": sub_file.stat().st_size,
                        "unknown_future_field": "ignore-me",
                    }
                }),
                encoding="utf-8",
            )

            entry = subtitle_cache._entry_from_raw({
                "key": "crc:DEADBEEF",
                "lang": "fr",
                "path": str(sub_file),
                "unexpected": {"nested": True},
            })
            self.assertEqual(entry.key, "crc:DEADBEEF")

            dest = Path(tmp) / "dest"
            with (
                patch.object(subtitle_cache, "_CACHE_DIR", str(cache_dir)),
                patch.object(subtitle_cache, "_INDEX_PATH", str(index_path)),
            ):
                res = subtitle_cache.get_cached_subtitle("crc:DEADBEEF", str(dest), lang="fr")

            self.assertIsNotNone(res)
            copied_path, loaded_entry = res
            self.assertTrue(os.path.isfile(copied_path))
            self.assertEqual(loaded_entry.key, "crc:DEADBEEF")


class FCJobStoreAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_try_mark_failed_is_atomic_and_has_processing_updates(self):
        from services.fc_job_store import FCJob, FCJobStore

        with tempfile.TemporaryDirectory() as tmp:
            store = FCJobStore(path=str(Path(tmp) / "fc_jobs.json"))
            job = FCJob(
                job_id="fc-1",
                uid=42,
                fname="ep1.mkv",
                output_name="ep1_out.mp4",
            )
            await store.add(job)
            self.assertTrue(await store.has_processing())

            first = await store.try_mark_failed("fc-1", "encode failed <bad>")
            second = await store.try_mark_failed("fc-1", "duplicate notification")
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertFalse(await store.has_processing())
            stored = await store.get("fc-1")
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, "failed")
            self.assertEqual(stored.error, "encode failed <bad>")

            # Uploaded job cannot transition to failed
            uploaded_job = FCJob(
                job_id="fc-2",
                uid=42,
                fname="ep2.mkv",
                output_name="ep2_out.mp4",
                uploaded=True,
            )
            await store.add(uploaded_job)
            self.assertFalse(await store.try_mark_failed("fc-2", "late error"))


class WebhookSyncAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_uid_falls_back_to_owner_id(self):
        from services import webhook_sync

        with patch.dict(os.environ, {"ADMIN_ID": ""}, clear=False):
            with patch("core.config.cfg", SimpleNamespace(owner_id=987654)):
                self.assertEqual(webhook_sync._admin_uid(), 987654)

        with patch.dict(os.environ, {"ADMIN_ID": "not_an_int"}, clear=False):
            with patch("core.config.cfg", SimpleNamespace(owner_id=555666)):
                self.assertEqual(webhook_sync._admin_uid(), 555666)

        with patch.dict(os.environ, {"ADMIN_ID": "111222"}, clear=False):
            self.assertEqual(webhook_sync._admin_uid(), 111222)

    async def test_poll_fc_pending_tries_multiple_keys_and_dedups_failure(self):
        from services import freeconvert_api, webhook_sync
        from services.fc_job_store import FCJob, FCJobStore
        import plugins.fc_webhook as fc_webhook

        with tempfile.TemporaryDirectory() as tmp:
            store = FCJobStore(path=str(Path(tmp) / "fc_jobs.json"))
            await store.add(
                FCJob(
                    job_id="fc-multi",
                    uid=77,
                    fname="ep.mkv",
                    output_name="ep.mp4",
                    api_key="",
                )
            )

            seen_keys: list[str] = []

            async def fake_get_job(api_key: str, job_id: str) -> dict:
                seen_keys.append(api_key)
                if api_key == "k1":
                    raise RuntimeError("wrong key")
                return {"id": job_id, "status": "failed", "tasks": []}

            notify_mock = AsyncMock()
            cleanup_mock = AsyncMock()

            with (
                patch("services.fc_job_store.fc_job_store", store),
                patch.object(freeconvert_api, "get_fc_api_keys", return_value=["k1", "k2"]),
                patch.object(freeconvert_api, "_fc_get_job", side_effect=fake_get_job),
                patch.object(fc_webhook, "_notify_failure", notify_mock),
                patch.object(fc_webhook, "_cleanup_seedr_source", cleanup_mock),
            ):
                await webhook_sync._poll_fc_pending()
                await webhook_sync._poll_fc_pending()

            self.assertEqual(seen_keys, ["k1", "k2"])
            notify_mock.assert_awaited_once()
            cleanup_mock.assert_awaited_once()


class TaskRunnerAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_render_panel_counts_owner_queued_prefix_as_queued(self):
        from services import task_runner

        rec = task_runner.TaskRecord(
            tid="t1",
            user_id=10,
            label="Encode",
            mode="proc",
            state="⏳ Queued · owner slots busy",
        )
        with (
            patch.object(task_runner.tracker, "all_tasks", return_value=[rec]),
            patch("core.bot_name.get_bot_name", return_value="Zilong"),
        ):
            panel = await task_runner.render_panel()

        self.assertIn("<b>0 running</b>  ·  1 queued", panel)
        self.assertIn(f"0/{task_runner.MAX_CONCURRENT}</code> slots", panel)

    async def test_run_task_marks_queued_when_semaphore_locked(self):
        from services import task_runner

        states: list[str] = []
        orig_update = task_runner.TaskRecord.update

        def spy_update(self, **kw):
            if "state" in kw:
                states.append(kw["state"])
            return orig_update(self, **kw)

        sem = asyncio.Semaphore(1)
        await sem.acquire()

        ran = asyncio.Event()

        async def work(_record):
            ran.set()

        runner = task_runner.TaskRunner()
        rec = task_runner.TaskRecord(tid="t2", user_id=10, label="Work", mode="dl", state="🆕 Init")

        with (
            patch.object(task_runner, "_get_semaphore", return_value=sem),
            patch.object(task_runner.TaskRecord, "update", spy_update),
        ):
            task = asyncio.create_task(runner._run_task(rec, work))
            await asyncio.sleep(0.02)
            self.assertIn("⏳ Queued", states)
            self.assertFalse(ran.is_set())
            sem.release()
            await task

        self.assertTrue(ran.is_set())
        self.assertIn("⚙️ Running", states)
        self.assertEqual(states[-1], "✅ Done")


class FFmpegAndFreeConvertAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_merge_videos_normalizes_aspect_ratio_on_reencode_fallback(self):
        from services import ffmpeg

        cmds: list[list[str]] = []

        async def fake_run(cmd: list[str], label: str = "FFmpeg", timeout: int = 0):
            cmds.append(cmd)
            if len(cmds) == 1:
                raise RuntimeError("concat copy mismatch")

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ffmpeg, "_run", side_effect=fake_run):
                await ffmpeg.merge_videos(
                    [os.path.join(tmp, "a.mp4"), os.path.join(tmp, "b.mp4")],
                    os.path.join(tmp, "out.mp4"),
                    tmp,
                )

        self.assertEqual(len(cmds), 2)
        fc_idx = cmds[1].index("-filter_complex") + 1
        filter_complex = cmds[1][fc_idx]
        self.assertIn("scale=1280:720:force_original_aspect_ratio=decrease", filter_complex)
        self.assertIn("pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1", filter_complex)

    async def test_submit_compress_forwards_output_format(self):
        from services import freeconvert_api

        captured: dict = {}

        async def fake_create(api_key: str, **kwargs):
            captured.update(kwargs)
            return "fc-comp-1"

        with (
            patch.object(freeconvert_api, "create_compress_job", side_effect=fake_create),
            patch.object(freeconvert_api, "upload_file_to_task", AsyncMock()),
        ):
            jid = await freeconvert_api.submit_compress(
                "fc-key",
                video_url="https://example.com/in.mkv",
                output_name="episode_01.mkv",
                target_mb=120.0,
            )

        self.assertEqual(jid, "fc-comp-1")
        self.assertEqual(captured.get("output_format"), "mkv")


class FastUploadAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_file_closes_fp_on_error_and_propagates_worker_error(self):
        import importlib
        import pyrogram
        import pyrogram.session
        for _sub in ("pyrogram.raw", "pyrogram.raw.functions", "pyrogram.raw.types"):
            try:
                importlib.import_module(_sub)
            except Exception:
                pass
        from pyrogram.client import Client
        import plugins.zz_fast_upload as zz_fast_upload

        orig_save = Client.save_file
        self.addCleanup(setattr, Client, "save_file", orig_save)
        self.addCleanup(setattr, zz_fast_upload, "INSTALLED", zz_fast_upload.INSTALLED)

        upload_ns = SimpleNamespace(
            SaveFilePart=lambda **kw: kw,
            SaveBigFilePart=lambda **kw: kw,
        )
        raw_types_ns = SimpleNamespace(
            InputFile=lambda **kw: SimpleNamespace(**kw),
            InputFileBig=lambda **kw: SimpleNamespace(**kw),
        )

        class _BoomSession:
            def __init__(self, *args, **kwargs):
                pass

            async def start(self):
                raise RuntimeError("session start failed")

            async def stop(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "tiny.bin"
            sample.write_bytes(b"hello-world")

            opened_fps = []
            real_open = open

            def track_open(*args, **kwargs):
                fh = real_open(*args, **kwargs)
                if str(args[0]) == str(sample):
                    opened_fps.append(fh)
                return fh

            zz_fast_upload.INSTALLED = False
            with (
                patch.object(pyrogram.session, "Session", _BoomSession),
                patch.object(pyrogram.raw.functions, "upload", upload_ns, create=True),
                patch.object(pyrogram.raw, "types", raw_types_ns),
                patch.dict(os.environ, {"TG_UPLOAD_BOOST": "1"}, clear=False),
            ):
                zz_fast_upload.install_fast_upload()

                dummy_client = SimpleNamespace(
                    save_file_semaphore=asyncio.Semaphore(1),
                    me=SimpleNamespace(is_premium=False),
                    rnd_id=lambda: 123456,
                    loop=asyncio.get_running_loop(),
                    executor=None,
                    storage=SimpleNamespace(
                        dc_id=AsyncMock(return_value=2),
                        auth_key=AsyncMock(return_value=b"k" * 256),
                        test_mode=AsyncMock(return_value=False),
                    ),
                )
                with patch("builtins.open", side_effect=track_open):
                    with self.assertRaises(RuntimeError):
                        await Client.save_file(dummy_client, str(sample))
            self.assertEqual(len(opened_fps), 1)
            self.assertTrue(opened_fps[0].closed)

            # Now test worker error propagation past bounded retry
            class _FailInvokeSession:
                def __init__(self, *args, **kwargs):
                    pass

                async def start(self):
                    return None

                async def stop(self):
                    return None

                async def invoke(self, _data):
                    raise RuntimeError("rpc part failed")

            zz_fast_upload.INSTALLED = False
            with (
                patch.object(pyrogram.session, "Session", _FailInvokeSession),
                patch.object(pyrogram.raw.functions, "upload", upload_ns, create=True),
                patch.object(pyrogram.raw, "types", raw_types_ns),
                patch.dict(os.environ, {"TG_UPLOAD_BOOST": "1"}, clear=False),
            ):
                zz_fast_upload.install_fast_upload()

                with patch("plugins.zz_fast_upload.asyncio.sleep", AsyncMock()):
                    with self.assertRaisesRegex(RuntimeError, "rpc part failed"):
                        await Client.save_file(dummy_client, str(sample))


class UploaderAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_file_missing_path_raises_and_escapes_html(self):
        from services import uploader

        msg = SimpleNamespace(
            id=9001,
            chat=SimpleNamespace(id=9100),
            from_user=SimpleNamespace(id=200),
            edit=AsyncMock(),
        )
        client = SimpleNamespace()
        missing = os.path.join(tempfile.gettempdir(), "missing_<bad>&file.mp4")

        with self.assertRaises(FileNotFoundError):
            await uploader.upload_file(client, msg, missing)

        msg.edit.assert_awaited_once()
        edited_text = msg.edit.await_args.args[0]
        self.assertIn("&lt;bad&gt;&amp;file.mp4", edited_text)

    async def test_split_all_parts_failed_raises_runtime_error(self):
        from services import uploader

        with tempfile.TemporaryDirectory() as tmp:
            big_dummy = Path(tmp) / "big_test.bin"
            big_dummy.write_bytes(b"12345")
            part1 = Path(tmp) / "part1.bin"
            part1.write_bytes(b"12")

            msg = SimpleNamespace(
                id=9002,
                chat=SimpleNamespace(id=9101),
                from_user=SimpleNamespace(id=200),
                edit=AsyncMock(),
            )
            client = SimpleNamespace(send_message=AsyncMock())

            with (
                patch.object(uploader, "TG_MAX_BYTES", 2),
                patch.object(uploader, "_split_binary", AsyncMock(return_value=[str(part1)])),
                patch.object(uploader, "_upload_single", AsyncMock(side_effect=RuntimeError("tg err"))),
            ):
                with self.assertRaisesRegex(RuntimeError, "All 1 parts failed"):
                    await uploader._upload_file_unlocked(
                        client, msg, str(big_dummy), force_document=True,
                    )

    def test_upload_text_helpers_escape_html(self):
        from services import uploader

        done = uploader._upload_complete_text("a<b&c>.mp4")
        self.assertIn("<code>a&lt;b&amp;c&gt;.mp4</code>", done)


class HandlersAndPermissionsAuditTests(unittest.IsolatedAsyncioTestCase):
    def test_wait_collectors_stay_in_group_2(self):
        self.assertEqual(_message_handler_group("plugins/start.py", "botname_collector"), 2)
        self.assertEqual(_message_handler_group("plugins/start.py", "cn_name_collector"), 2)
        self.assertEqual(_message_handler_group("plugins/start.py", "prefix_suffix_collector"), 2)
        self.assertEqual(_message_handler_group("plugins/start.py", "af_channel_collector"), 2)
        self.assertEqual(_message_handler_group("plugins/caption_templates.py", "ctpl_input_receiver"), 2)
        self.assertEqual(_message_handler_group("plugins/forwarder.py", "fwd_target"), 2)

    async def test_start_af_info_and_runtime_multi_fc_key(self):
        from core.session import settings
        from services import engine_router, freeconvert_api
        import plugins.start as start_mod

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            data="af_info|0",
            answer=AsyncMock(),
            message=SimpleNamespace(id=9003, chat=SimpleNamespace(id=9102), edit=AsyncMock()),
        )
        fake_settings = {
            "forward_channels": [{"id": -100123, "name": "My Channel"}],
            "seedr_cleanup": "success",
            "progress_style": "A",
            "progress_frequency": "high",
            "thumb_quality": "hd",
            "upload_mode": "auto",
            "prefix": "",
            "suffix": "",
        }
        with patch.object(settings, "get", AsyncMock(return_value=fake_settings)):
            await start_mod.cq_af_info(None, cb)
            cb.answer.assert_awaited_once()
            self.assertIn("My Channel", cb.answer.await_args.args[0])
            self.assertTrue(cb.answer.await_args.kwargs.get("show_alert"))

            cb.answer.reset_mock()
            cb.data = "af_info|5"
            await start_mod.cq_af_info(None, cb)
            cb.answer.assert_awaited_once_with("Channel not found", show_alert=True)

            cb.answer.reset_mock()
            cb.data = "cb_runtime"
            with (
                patch.object(
                    engine_router,
                    "get_hardsub_decision",
                    AsyncMock(return_value=SimpleNamespace(engine="freeconvert", reason="auto")),
                ),
                patch.object(freeconvert_api, "get_fc_api_keys", return_value=["fc-k1", "fc-k2"]),
                patch.dict(os.environ, {"FC_API_KEY": "", "CC_API_KEY": ""}, clear=False),
            ):
                await start_mod.cq_runtime(None, cb)

            edited = cb.message.edit.await_args.args[0]
            self.assertIn("<b>FC key:</b> <code>yes</code>", edited)

    async def test_resize_cancel_cleans_tmp_and_rejects_invalid_height(self):
        import plugins.resize as resize_mod

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp) / "job_tmp"
            tmp_dir.mkdir()
            (tmp_dir / "temp.part").write_bytes(b"abc")
            resize_mod._cz_pending["tok1"] = {
                "tmp": str(tmp_dir),
                "path": str(tmp_dir / "temp.part"),
                "fname": "a.mp4",
                "target_mb": 10.0,
                "uid": 99,
                "_ts": 9999999999.0,
            }
            cb = SimpleNamespace(
                data="czp|cancel|tok1",
                from_user=SimpleNamespace(id=99),
                answer=AsyncMock(),
                message=SimpleNamespace(id=9004, chat=SimpleNamespace(id=9103), delete=AsyncMock()),
            )
            await resize_mod.czp_cb(None, cb)
            self.assertNotIn("tok1", resize_mod._cz_pending)
            self.assertFalse(tmp_dir.exists())
            cb.message.delete.assert_awaited_once()

        for idx, bad_height in enumerate(("0", "-480", "abc")):
            msg = SimpleNamespace(id=9010 + idx, chat=SimpleNamespace(id=9104), edit=AsyncMock())
            await resize_mod._rsz_work(None, msg, {"kind": "url"}, bad_height, "t", 99)
            msg.edit.assert_awaited()
            self.assertIn("Invalid resolution", msg.edit.await_args.args[0])

    async def test_jobs_debug_enforces_ownership_and_extras_escapes_html(self):
        import plugins.extras as extras_mod
        import plugins.jobs_debug as jobs_debug

        other_cc = SimpleNamespace(
            job_id="cc-1", uid=111, status="processing", fname="<a.mkv>",
            sub_fname="s.ass", output_name="o.mp4", uploaded=False,
            notified=False, export_url="", seedr_folder_id=0,
            error_msg="", task_message="",
        )
        other_fc = SimpleNamespace(
            job_id="fc-1", uid=111, status="processing", job_type="hardsub",
            fname="<b>", sub_fname="", output_name="o.mp4", uploaded=False,
            seedr_folder_id=0, error="",
        )

        msg = SimpleNamespace(
            command=["debugjob", "cc-1"],
            from_user=SimpleNamespace(id=222),
            reply=AsyncMock(),
        )
        with (
            patch.object(jobs_debug, "cfg", SimpleNamespace(owner_id=1)),
            patch.object(jobs_debug.cc_job_store, "get", return_value=other_cc),
            patch.object(jobs_debug.fc_job_store, "get", AsyncMock(return_value=other_fc)),
        ):
            await jobs_debug.cmd_debugjob(None, msg)
            self.assertIn("No stored job", msg.reply.await_args.args[0])

            # Same user as job uid succeeds and HTML-escapes fields
            msg.from_user.id = 111
            msg.reply.reset_mock()
            await jobs_debug.cmd_debugjob(None, msg)
            text = msg.reply.await_args.args[0]
            self.assertIn("<b>Job Debug</b>", text)
            self.assertIn("&lt;a.mkv&gt;", text)
            self.assertIn("&lt;b&gt;", text)

        json_msg = SimpleNamespace(
            command=["json_formatter", '{"k": "<script>alert(1)</script>"}'],
            reply_to_message=None,
            reply=AsyncMock(),
        )
        await extras_mod.cmd_json(None, json_msg)
        reply_html = json_msg.reply.await_args.args[0]
        self.assertIn("&lt;script&gt;", reply_html)
        self.assertNotIn("<script>", reply_html)

    async def test_caption_templates_cb_blocks_unauthorized_channel(self):
        import plugins.caption_templates as ctpl

        cb = SimpleNamespace(
            data="ctpl|edit|-100999",
            from_user=SimpleNamespace(id=222),
            answer=AsyncMock(),
            message=SimpleNamespace(id=9020, chat=SimpleNamespace(id=9105), edit=AsyncMock(), delete=AsyncMock()),
        )
        with (
            patch.object(ctpl, "cfg", SimpleNamespace(owner_id=1)),
            patch.object(ctpl, "_channels_with_templates", AsyncMock(return_value=[{"id": -100111, "name": "Own"}])),
        ):
            await ctpl.ctpl_cb(None, cb)

        cb.answer.assert_awaited_once_with("Not authorized for this channel.", show_alert=True)
        cb.message.edit.assert_not_awaited()
