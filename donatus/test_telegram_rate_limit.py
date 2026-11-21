import unittest
import asyncio
import time

from services.telegram_rate_limiter import TelegramRateLimiter, rate_limiter
from services.utils import PanelUpdater, important_edit, safe_edit


class FakeChat:
    def __init__(self, cid): self.id = cid


class FakeMsg:
    def __init__(self, cid=1, mid=1):
        self.chat = FakeChat(cid)
        self.id = mid
        self.edits = []
    async def edit(self, text, **kwargs):
        self.edits.append(text)
        return self
    async def delete(self):
        return True


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_bucket_limits(self):
        rl = TelegramRateLimiter()
        rl.edit_rate = 0.1
        rl.edit_burst = 1
        msg = FakeMsg()
        ok1 = await rl.safe_edit_message(msg, 'a')
        ok2 = await rl.safe_edit_message(msg, 'b')
        self.assertTrue(ok1)
        self.assertFalse(ok2)

    async def test_important_lifecycle_edit_is_not_dropped(self):
        msg = FakeMsg(cid=501, mid=901)
        old_interval = rate_limiter.global_min_edit_interval
        rate_limiter.global_min_edit_interval = 0.01
        try:
            await safe_edit(msg, "Submitting")
            ok = await important_edit(msg, "Provider processing", wait_timeout=0.5)
        finally:
            rate_limiter.global_min_edit_interval = old_interval

        self.assertTrue(ok)
        self.assertEqual(msg.edits, ["Submitting", "Provider processing"])

    async def test_clear_edit_throttle_only_resets_one_message_interval(self):
        rl = TelegramRateLimiter()
        rl.global_min_edit_interval = 60.0
        msg = FakeMsg(cid=7, mid=8)

        self.assertTrue(await rl.safe_edit_message(msg, "first"))
        self.assertFalse(await rl.safe_edit_message(msg, "blocked"))

        chat_cooldown = time.monotonic() + 60.0
        global_cooldown = time.monotonic() + 30.0
        rl._chat_cooldown[7] = chat_cooldown
        rl._global_cooldown = global_cooldown
        bucket = rl._chat_buckets[7]
        bucket_tokens = bucket.tokens

        self.assertTrue(rl.clear_edit_throttle(msg))
        self.assertEqual(rl._chat_cooldown[7], chat_cooldown)
        self.assertEqual(rl._global_cooldown, global_cooldown)
        self.assertIs(rl._chat_buckets[7], bucket)
        self.assertEqual(bucket.tokens, bucket_tokens)

        # Remove the deliberately injected cooldowns so the next assertion
        # isolates the per-message interval reset.
        rl._chat_cooldown[7] = 0.0
        rl._global_cooldown = 0.0
        self.assertTrue(await rl.safe_edit_message(msg, "next stage"))
        self.assertEqual(msg.edits[-1], "next stage")

    def test_clear_edit_throttle_rejects_message_without_identity(self):
        rl = TelegramRateLimiter()
        self.assertFalse(rl.clear_edit_throttle(object()))

    async def test_panel_coalesces(self):
        msg = FakeMsg()
        pu = PanelUpdater(msg, lambda s: f"{s.get('done',0)}", interval=0.2, min_interval=0.2, min_percent_delta=100, min_bytes_delta=1<<30)
        await pu.start()
        for i in range(30):
            pu.tick(done=i,total=100)
        await asyncio.sleep(0.35)
        await pu.stop()
        self.assertLessEqual(len(msg.edits), 3)

    async def test_no_repeat_100(self):
        msg = FakeMsg()
        pu = PanelUpdater(msg, lambda s: f"{s.get('done',0)}", interval=0.2, min_interval=0.2)
        await pu.start()
        pu.tick(done=100,total=100)
        await asyncio.sleep(0.25)
        pu.tick(done=100,total=100)
        await asyncio.sleep(0.25)
        await pu.stop()
        self.assertLessEqual(msg.edits.count('100'), 1)

    def test_intervals_not_one(self):
        import pathlib
        self.assertIn(
            'interval=3.0',
            pathlib.Path('services/tg_download.py').read_text(encoding='utf-8'),
        )
        self.assertNotIn(
            'interval=1.0',
            pathlib.Path('services/downloader.py').read_text(encoding='utf-8'),
        )

if __name__ == '__main__':
    unittest.main()
