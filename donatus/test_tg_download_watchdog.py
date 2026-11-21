import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock

from services.tg_download import tg_download
from services.telegram_rate_limiter import rate_limiter, safe_edit_message


class _Chat:
    def __init__(self, chat_id=424242):
        self.id = chat_id


class _Msg:
    _next_id = 10000

    def __init__(self, chat_id=424242):
        self.chat = _Chat(chat_id)
        _Msg._next_id += 1
        self.id = _Msg._next_id
        self.edits = []

    async def edit(self, text, **kwargs):
        self.edits.append((text, kwargs))
        return self


class _StallingClient:
    async def download_media(self, file_id, file_name=None, progress=None):
        if progress:
            await progress(132, 136)
        with open(file_name, "wb") as fh:
            fh.write(b"x" * 132)
        await asyncio.sleep(60)


class _RetryClient:
    def __init__(self):
        self.calls = 0

    async def download_media(self, file_id, file_name=None, progress=None):
        self.calls += 1
        if self.calls == 1:
            if progress:
                await progress(132, 136)
            with open(file_name, "wb") as fh:
                fh.write(b"x" * 132)
            await asyncio.sleep(60)
        if progress:
            await progress(136, 136)
        with open(file_name, "wb") as fh:
            fh.write(b"x" * 136)
        return file_name


class _CompleteClient:
    async def download_media(self, file_id, file_name=None, progress=None):
        if progress:
            await progress(4, 4)
        with open(file_name, "wb") as fh:
            fh.write(b"done")
        return file_name


class TelegramDownloadWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_telegram_download_fails_instead_of_hanging(self):
        with tempfile.TemporaryDirectory() as td:
            dest = os.path.join(td, "video.mkv")
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "stalled"):
                await tg_download(
                    _StallingClient(), "file-id", dest, _Msg(),
                    fname="video.mkv", fsize=136, user_id=123,
                    stall_timeout=0.15, retries=0,
                )
            self.assertLess(time.monotonic() - started, 2.0)

    async def test_stalled_telegram_download_retries_and_completes(self):
        with tempfile.TemporaryDirectory() as td:
            dest = os.path.join(td, "video.mkv")
            client = _RetryClient()
            path = await tg_download(
                client, "file-id", dest, _Msg(),
                fname="video.mkv", fsize=136, user_id=123,
                stall_timeout=0.15, retries=1,
            )
            self.assertEqual(path, dest)
            self.assertEqual(client.calls, 2)
            self.assertEqual(os.path.getsize(path), 136)

    async def test_success_collapses_panel_and_allows_immediate_next_stage(self):
        with tempfile.TemporaryDirectory() as td:
            dest = os.path.join(td, "video.mkv")
            msg = _Msg(chat_id=515151)
            path = await tg_download(
                _CompleteClient(), "file-id", dest, msg,
                fname="video.mkv", fsize=4, user_id=123,
                stall_timeout=0.15, retries=0,
            )

            self.assertEqual(path, dest)
            self.assertEqual(msg.edits[-1][0], "✅ Downloaded")
            self.assertNotIn((msg.chat.id, msg.id), rate_limiter._msg_last_edit)

            self.assertTrue(
                await safe_edit_message(msg, "📡 Reading streams…", mode="progress")
            )
            self.assertEqual(msg.edits[-1][0], "📡 Reading streams…")

    async def test_terminal_panel_error_does_not_fail_completed_download(self):
        with tempfile.TemporaryDirectory() as td:
            dest = os.path.join(td, "video.mkv")
            msg = _Msg(chat_id=525252)
            with mock.patch(
                "services.tg_download.safe_edit_message",
                new=mock.AsyncMock(side_effect=RuntimeError("edit unavailable")),
            ):
                path = await tg_download(
                    _CompleteClient(), "file-id", dest, msg,
                    fname="video.mkv", fsize=4, user_id=123,
                    stall_timeout=0.15, retries=0,
                )

            self.assertEqual(path, dest)
            self.assertEqual(os.path.getsize(path), 4)
            self.assertNotIn((msg.chat.id, msg.id), rate_limiter._msg_last_edit)


if __name__ == "__main__":
    unittest.main()
