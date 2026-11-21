"""Regression tests for multi-user isolation, scaling, and FloodWait protection."""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import AsyncExitStack, nullcontext
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


class _FakeChat:
    def __init__(self, cid: int) -> None:
        self.id = cid


class _FakeMsg:
    def __init__(self, cid: int, mid: int) -> None:
        self.chat = _FakeChat(cid)
        self.id = mid
        self.edits: list[str] = []

    async def edit(self, text: str, **kwargs):
        self.edits.append(text)
        return self

    async def delete(self):
        return True


class RateLimiterScaleTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_edit_bucket_caps_edits_across_chats(self):
        from services.telegram_rate_limiter import TelegramRateLimiter

        rl = TelegramRateLimiter()
        rl.edit_burst = 10.0
        rl.global_edit_rate = 0.01
        rl.global_edit_burst = 2.0

        self.assertTrue(await rl.safe_edit_message(_FakeMsg(101, 1), "u1"))
        self.assertTrue(await rl.safe_edit_message(_FakeMsg(102, 1), "u2"))
        # 3rd distinct chat hits the shared bot-wide global edit ceiling
        self.assertFalse(await rl.safe_edit_message(_FakeMsg(103, 1), "u3"))

    async def test_safe_send_message_respects_flood_cooldown_and_copy_retries(self):
        from services.telegram_rate_limiter import TelegramRateLimiter

        rl = TelegramRateLimiter()
        rl.mark_flood(55, 4)
        fake_client = SimpleNamespace(send_message=AsyncMock(return_value="sent"))

        # Progress-mode sends immediately drop during cooldown without RPC call
        res_prog = await rl.safe_send_message(fake_client, 55, "hi", mode="progress")
        self.assertIsNone(res_prog)
        self.assertEqual(fake_client.send_message.await_count, 0)

        # Normal-mode short cooldown waits and sends once cooldown lifts
        rl2 = TelegramRateLimiter()
        rl2._chat_cooldown[77] = time.monotonic() + 0.05
        res_norm = await rl2.safe_send_message(fake_client, 77, "ok", mode="normal")
        self.assertEqual(res_norm, "sent")
        self.assertEqual(fake_client.send_message.await_count, 1)

        # safe_copy_message marks FloodWait and waits out cooldown before retry
        attempts = {"n": 0}

        async def _copy(uid, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("FLOOD_WAIT_1")
            return f"copied:{uid}"

        fake_bcast = SimpleNamespace(copy=_copy)
        t0 = time.monotonic()
        copied = await rl2.safe_copy_message(fake_bcast, 88, wait_timeout=10.0)
        elapsed = time.monotonic() - t0
        self.assertEqual(copied, "copied:88")
        self.assertEqual(attempts["n"], 2)
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertGreaterEqual(len(rl2._flood_events[88]), 1)

    async def test_dead_message_tracked_and_stale_entries_pruned(self):
        from services.telegram_rate_limiter import TelegramRateLimiter

        rl = TelegramRateLimiter()

        class _DeadMsg(_FakeMsg):
            async def edit(self, text: str, **kwargs):
                raise RuntimeError("MESSAGE_ID_INVALID")

        dead = _DeadMsg(301, 9)
        self.assertFalse(await rl.safe_edit_message(dead, "x", mode="progress"))
        self.assertTrue(rl.is_dead_message(dead))

        now = time.monotonic()
        rl._msg_last_edit[(999, 1)] = now - 7200.0
        rl._msg_locks[(999, 1)] = asyncio.Lock()
        rl._op_counter = 127
        rl._maybe_prune(now)
        self.assertNotIn((999, 1), rl._msg_last_edit)
        self.assertNotIn((999, 1), rl._msg_locks)


class PanelAndTmpMultiUserTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_updater_scales_interval_with_concurrent_panels(self):
        from services import utils as _utils
        from services.utils import PanelUpdater

        _utils._ACTIVE_PANEL_UPDATERS.clear()
        p1 = PanelUpdater(_FakeMsg(1, 1), lambda s: f"{s}", interval=3.0)
        await p1.start()
        try:
            self.assertEqual(p1._scaled_sleep_interval(), 3.0)
            others = [PanelUpdater(_FakeMsg(i, 1), lambda s: f"{s}", interval=3.0) for i in range(2, 7)]
            for pu in others:
                await pu.start()
            try:
                self.assertGreater(p1._scaled_sleep_interval(), 3.0)
                self.assertLessEqual(p1._scaled_sleep_interval(), p1._MAX_EFFECTIVE_INTERVAL)
            finally:
                for pu in others:
                    await pu.stop()
        finally:
            await p1.stop()

    def test_make_tmp_is_unique_even_under_same_ms_for_same_user(self):
        from services.utils import cleanup, make_tmp

        with tempfile.TemporaryDirectory() as tmp:
            paths = {make_tmp(tmp, 777) for _ in range(40)}
            self.assertEqual(len(paths), 40)
            for p in paths:
                self.assertTrue(os.path.isdir(p))
                cleanup(p)


class SessionStorePerfTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_register_and_session_get_avoid_redundant_disk_writes(self):
        from core.session import SessionStore, UserStore

        with tempfile.TemporaryDirectory() as tmp:
            ustore = UserStore()
            ustore._data.clear()
            ustore._path = os.path.join(tmp, "users.json")
            sstore = SessionStore()
            sstore._data.clear()
            sstore._path = os.path.join(tmp, "sessions.json")

            u_saves = 0
            orig_usave = ustore._save

            def count_usave():
                nonlocal u_saves
                u_saves += 1
                orig_usave()

            ustore._save = count_usave
            await ustore.register(42, "Alice")
            self.assertEqual(u_saves, 1)
            for _ in range(10):
                await ustore.register(42, "Alice")
            self.assertEqual(u_saves, 1)
            await ustore.register(42, "Alice2")
            self.assertEqual(u_saves, 2)

            s_saves = 0
            orig_ssave = sstore._save

            def count_ssave():
                nonlocal s_saves
                s_saves += 1
                orig_ssave()

            sstore._save = count_ssave
            sess = await sstore.create(42, "fid", "video.mkv", 1024, ".mkv", tmp)
            self.assertEqual(s_saves, 1)
            for _ in range(10):
                self.assertIsNotNone(sstore.get(sess.key))
            self.assertEqual(s_saves, 1)
            # Only persists TTL touch once age >= 30s
            sess.created = time.time() - 35.0
            self.assertIsNotNone(sstore.get(sess.key))
            self.assertEqual(s_saves, 2)


class GlobalConcurrencyBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_limiter_enforces_global_cap_across_chats(self):
        from services.upload_limiter import ChatUploadLimiter

        lim = ChatUploadLimiter(max_concurrent=2, global_max=3)
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(lim.slot(10))
            await stack.enter_async_context(lim.slot(10))
            self.assertTrue(lim.will_queue(10))
            self.assertFalse(lim.will_queue(20))

            await stack.enter_async_context(lim.slot(20))
            # Global limit 3 reached -> even fresh chat 30 must queue
            self.assertTrue(lim.will_queue(30))

            entered = asyncio.Event()
            release = asyncio.Event()

            async def _chat30():
                async with lim.slot(30):
                    entered.set()
                    await release.wait()

            t = asyncio.create_task(_chat30())
            await asyncio.sleep(0.05)
            self.assertFalse(entered.is_set())
            self.assertEqual(lim.snapshot()["global_active"], 3)
            self.assertEqual(lim.snapshot()["global_waiting"], 1)

            await stack.aclose()
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            release.set()
            await asyncio.wait_for(t, timeout=1.0)

        snap = lim.snapshot()
        self.assertEqual(snap["global_active"], 0)
        self.assertEqual(snap["global_waiting"], 0)
        self.assertEqual(snap["tracked_chats"], 0)

    async def test_ffmpeg_runner_caps_simultaneous_subprocesses(self):
        from services import ffmpeg_runner

        active = {"cur": 0, "max": 0}
        release = asyncio.Event()

        class _EmptyStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class _FakeProc:
            def __init__(self, pid: int):
                self.pid = pid
                self.returncode = 0
                self.stdout = _EmptyStream()
                self.stderr = _EmptyStream()

            async def wait(self):
                await release.wait()
                active["cur"] -= 1
                return 0

        next_pid = {"v": 100}

        async def _fake_exec(*args, **kwargs):
            active["cur"] += 1
            active["max"] = max(active["max"], active["cur"])
            next_pid["v"] += 1
            return _FakeProc(next_pid["v"])

        class _FakeTaskRecord:
            def __init__(self):
                self.updates = []

            def update(self, **kw):
                self.updates.append(kw)

        recs = [_FakeTaskRecord() for _ in range(4)]

        with (
            patch.dict(os.environ, {"ZILONG_FFMPEG_MAX_CONCURRENT": "2"}, clear=False),
            patch("services.ffmpeg_runner.asyncio.create_subprocess_exec", side_effect=_fake_exec),
        ):
            ffmpeg_runner._FFMPEG_SEMS.clear()
            tasks = [
                asyncio.create_task(ffmpeg_runner.run_ffmpeg(["ffmpeg", "-version"], task_record=r))
                for r in recs
            ]
            await asyncio.sleep(0.08)
            self.assertEqual(active["max"], 2)
            self.assertTrue(any(u.get("state") == "Queued" for r in recs for u in r.updates))
            release.set()
            results = await asyncio.gather(*tasks)
            self.assertTrue(all(res.ok for res in results))

    async def test_fast_upload_coordinates_floodwait_backoff(self):
        import pyrogram
        import pyrogram.session
        for _sub in ("pyrogram.raw", "pyrogram.raw.functions", "pyrogram.raw.types"):
            try:
                importlib.import_module(_sub)
            except Exception:
                pass
        from pyrogram.client import Client
        from plugins import zz_fast_upload

        orig_save = Client.save_file
        self.addCleanup(setattr, Client, "save_file", orig_save)
        self.addCleanup(setattr, zz_fast_upload, "INSTALLED", zz_fast_upload.INSTALLED)

        sleep_calls: list[float] = []
        calls = {"n": 0}

        class _FloodOnceSession:
            def __init__(self, *args, **kwargs):
                pass

            async def start(self):
                return None

            async def stop(self):
                return None

            async def invoke(self, _data):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("FLOOD_WAIT_4")
                return None

        upload_ns = SimpleNamespace(
            SaveFilePart=lambda **kw: kw,
            SaveBigFilePart=lambda **kw: kw,
        )
        raw_types_ns = SimpleNamespace(
            InputFile=lambda **kw: SimpleNamespace(**kw),
            InputFileBig=lambda **kw: SimpleNamespace(**kw),
        )

        async def _fake_sleep(secs):
            sleep_calls.append(float(secs))

        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "part.bin"
            sample.write_bytes(b"abc123")
            zz_fast_upload.INSTALLED = False
            with (
                patch.object(pyrogram.session, "Session", _FloodOnceSession),
                patch.object(pyrogram.raw.functions, "upload", upload_ns, create=True),
                patch.object(pyrogram.raw, "types", raw_types_ns),
                patch.dict(os.environ, {"TG_UPLOAD_BOOST": "1", "TG_UPLOAD_SESSIONS": "1", "TG_UPLOAD_WORKERS": "2"}, clear=False),
                patch("plugins.zz_fast_upload.asyncio.sleep", side_effect=_fake_sleep),
            ):
                zz_fast_upload.install_fast_upload()
                dummy_client = SimpleNamespace(
                    save_file_semaphore=asyncio.Semaphore(1),
                    me=SimpleNamespace(is_premium=False),
                    rnd_id=lambda: 999999,
                    loop=asyncio.get_running_loop(),
                    executor=None,
                    storage=SimpleNamespace(
                        dc_id=AsyncMock(return_value=2),
                        auth_key=AsyncMock(return_value=b"k" * 256),
                        test_mode=AsyncMock(return_value=False),
                    ),
                )
                await Client.save_file(dummy_client, str(sample))

        self.assertGreaterEqual(calls["n"], 2)
        self.assertIn(5.0, sleep_calls)


class MultiUserUrlAndAdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_handler_cache_isolated_between_users(self):
        try:
            import yt_dlp  # noqa: F401
            ctx = nullcontext()
        except ImportError:
            ctx = patch.dict(sys.modules, {"yt_dlp": types.ModuleType("yt_dlp")})
        with ctx:
            from plugins import url_handler

            url_handler._cache.clear()
            tok = url_handler._store("https://example.com/shared-video")
            url_handler._set_user_cache(url_handler._ytinfo_key(1001, tok), '{"title":"A"}')
            url_handler._set_user_cache(url_handler._ytinfo_key(1002, tok), '{"title":"B"}')

            cb = SimpleNamespace(
                from_user=SimpleNamespace(id=1001),
                data=f"dl|cancel|{tok}",
                message=SimpleNamespace(delete=AsyncMock()),
                answer=AsyncMock(),
            )
            await url_handler.dl_cb(SimpleNamespace(), cb)
            self.assertNotIn(url_handler._ytinfo_key(1001, tok), url_handler._cache)
            self.assertEqual(url_handler._cache.get(url_handler._ytinfo_key(1002, tok)), '{"title":"B"}')

    async def test_admin_notify_and_broadcast_use_rate_limiter(self):
        admin = importlib.import_module("plugins.admin")

        sent_to: list[int] = []

        class _FakeUser:
            def __init__(self, uid: int, banned: bool = False):
                self.uid = uid
                self.banned = banned

        fake_client = SimpleNamespace(
            send_message=AsyncMock(return_value="ok"),
        )
        await admin._safe_notify(fake_client, 501, "hello")
        fake_client.send_message.assert_awaited_once()

        bcast_msg = SimpleNamespace(copy=AsyncMock(side_effect=lambda uid: sent_to.append(uid) or "copied"))
        st_msg = SimpleNamespace(chat=SimpleNamespace(id=1), id=77, edit=AsyncMock())
        cmd_msg = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            reply_to_message=bcast_msg,
            reply=AsyncMock(return_value=st_msg),
            stop_propagation=lambda: None,
        )

        with (
            patch.object(admin.users, "all_users", return_value=[_FakeUser(201), _FakeUser(202, banned=True), _FakeUser(203)]),
            patch.object(admin, "is_allowed", side_effect=lambda uid: uid in (201, 203)),
            patch("plugins.admin.asyncio.sleep", AsyncMock()),
        ):
            await admin.cmd_broadcast(fake_client, cmd_msg)

        self.assertEqual(sent_to, [201, 203])


class AltPortTransportTests(unittest.TestCase):
    def test_should_use_alt_port_detects_instabox_and_respects_override(self):
        from services import tg_transport

        self.assertFalse(tg_transport.should_use_alt_port({}))
        self.assertTrue(
            tg_transport.should_use_alt_port(
                {"SSL_CERT_FILE": "/run/instabox/secrets/ca-bundle.pem"}
            )
        )
        self.assertTrue(
            tg_transport.should_use_alt_port(
                {"NODE_EXTRA_CA_CERTS": "/usr/local/share/ca-certificates/instabox-proxy.crt"}
            )
        )
        self.assertTrue(tg_transport.should_use_alt_port({"TG_ALT_PORT": "1"}))
        self.assertFalse(
            tg_transport.should_use_alt_port(
                {"SSL_CERT_FILE": "/run/instabox/secrets/ca-bundle.pem", "TG_ALT_PORT": "0"}
            )
        )

    def test_enable_alt_port_globally_forces_true(self):
        from services import tg_transport

        dc_mod = types.ModuleType("pyrogram.session.internals.data_center")
        seen = []

        class _FakeDC:
            def __new__(cls, dc_id, test_mode, ipv6, alt_port, media=False):
                seen.append((dc_id, test_mode, ipv6, alt_port, media))
                return ("149.154.167.51", 5222 if alt_port else 443)

        dc_mod.DataCenter = _FakeDC
        with patch.dict(sys.modules, {"pyrogram.session.internals.data_center": dc_mod}):
            self.assertTrue(tg_transport.enable_alt_port_globally())
            self.assertEqual(_FakeDC(2, False, False, False, True), ("149.154.167.51", 5222))
            self.assertEqual(seen, [(2, False, False, True, True)])


if __name__ == "__main__":
    unittest.main()
