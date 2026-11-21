"""
tests/test_upload_limiter.py

Unit tests for the per-chat bounded upload limiter
(services/upload_limiter.py). This module has no pyrogram dependency, so the
concurrency behaviour can be exercised directly.

Covers the upload-concurrency requirements:
  * 1st, 2nd and 3rd uploads in a chat start without a queue/waiting notice.
  * The 4th upload is told to wait.
  * Up to 3 send stages run concurrently; a 4th blocks until a slot frees.
  * Post-upload work does NOT hold a slot (releasing frees capacity).
  * Limiter state is cleaned up once no uploads are active or waiting.
  * Max is configurable via ZILONG_MAX_CHAT_UPLOADS.
  * Admin reset clears tracked state.
"""
import asyncio
import os
import unittest
from contextlib import AsyncExitStack

from services.upload_limiter import ChatUploadLimiter


class WillQueueThresholdTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_three_do_not_queue_fourth_does(self):
        lim = ChatUploadLimiter(max_concurrent=3)
        chat = 100

        # No uploads yet → 1st upload does not queue.
        self.assertFalse(lim.will_queue(chat))

        async with lim.slot(chat):                 # 1 active
            self.assertFalse(lim.will_queue(chat)) # 2nd upload: no warning
            async with lim.slot(chat):             # 2 active
                self.assertFalse(lim.will_queue(chat))  # 3rd upload: no warning
                async with lim.slot(chat):         # 3 active
                    # 4th upload: all slots busy → must wait.
                    self.assertTrue(lim.will_queue(chat))

        # Everything released → back to no-queue and untracked.
        self.assertFalse(lim.will_queue(chat))
        self.assertFalse(lim.is_tracked(chat))

    async def test_queue_decision_is_per_chat(self):
        lim = ChatUploadLimiter(max_concurrent=3)
        async with AsyncExitStack() as stack:
            for _ in range(3):
                await stack.enter_async_context(lim.slot(1))
            self.assertTrue(lim.will_queue(1))    # chat 1 is full
            self.assertFalse(lim.will_queue(2))   # chat 2 is independent


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_fourth_blocks_until_a_slot_frees(self):
        lim = ChatUploadLimiter(max_concurrent=3)
        chat = 7
        async with AsyncExitStack() as stack:
            for _ in range(3):
                await stack.enter_async_context(lim.slot(chat))

            state = lim.chat_state(chat)
            self.assertEqual(state["active"], 3)
            self.assertTrue(lim.will_queue(chat))

            started = asyncio.Event()
            release = asyncio.Event()

            async def fourth_upload():
                async with lim.slot(chat):
                    started.set()
                    await release.wait()

            task = asyncio.create_task(fourth_upload())
            await asyncio.sleep(0.05)

            # The 4th upload cannot have started — all three slots are held.
            self.assertFalse(started.is_set())
            self.assertEqual(lim.chat_state(chat)["waiting"], 1)

            # Free one of the three held slots.
            await stack.aclose()

            # Now the 4th upload acquires its slot and runs.
            await asyncio.wait_for(started.wait(), timeout=1)
            release.set()
            await asyncio.wait_for(task, timeout=1)

        self.assertFalse(lim.is_tracked(chat))

    async def test_post_upload_work_does_not_hold_a_slot(self):
        """A slot is freed the instant the send stage exits, even if later
        background work for that upload is still running."""
        lim = ChatUploadLimiter(max_concurrent=1)
        chat = 9
        background_done = asyncio.Event()

        async def upload_with_background_hook():
            async with lim.slot(chat):
                pass  # send stage finished → slot released here
            # Simulate post-upload hooks running AFTER the slot is released.
            await asyncio.sleep(0.2)
            background_done.set()

        task = asyncio.create_task(upload_with_background_hook())
        await asyncio.sleep(0.05)

        # Background hook is still running, but the single slot is already free.
        self.assertFalse(background_done.is_set())
        self.assertFalse(lim.will_queue(chat))
        async with lim.slot(chat):       # acquires immediately — not blocked
            self.assertEqual(lim.chat_state(chat)["active"], 1)

        await asyncio.wait_for(task, timeout=1)
        self.assertTrue(background_done.is_set())


class CleanupAndConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_cleaned_up_after_uploads_finish(self):
        lim = ChatUploadLimiter(max_concurrent=2)
        chat = 42
        async with lim.slot(chat):
            self.assertTrue(lim.is_tracked(chat))
        # Idle chats leave no residue.
        self.assertFalse(lim.is_tracked(chat))
        self.assertEqual(lim.snapshot()["tracked_chats"], 0)
        self.assertEqual(lim.snapshot()["stale"], [])

    async def test_reset_clears_tracked_state(self):
        lim = ChatUploadLimiter(max_concurrent=3)
        async with AsyncExitStack() as stack:
            for _ in range(2):
                await stack.enter_async_context(lim.slot(500))
            self.assertTrue(lim.is_tracked(500))

            # Admin "unstick": reset forgets the entry; the in-flight slots
            # still release cleanly without raising.
            self.assertEqual(lim.reset(500), 1)
            self.assertFalse(lim.is_tracked(500))
            self.assertEqual(lim.reset(500), 0)  # nothing left to clear

    async def test_reset_all(self):
        lim = ChatUploadLimiter(max_concurrent=3)
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(lim.slot(1))
            await stack.enter_async_context(lim.slot(2))
            self.assertEqual(lim.snapshot()["tracked_chats"], 2)
            self.assertEqual(lim.reset(), 2)
            self.assertEqual(lim.snapshot()["tracked_chats"], 0)

    async def test_max_concurrent_env_override(self):
        prev = os.environ.get("ZILONG_MAX_CHAT_UPLOADS")
        try:
            os.environ["ZILONG_MAX_CHAT_UPLOADS"] = "5"
            self.assertEqual(ChatUploadLimiter().max_concurrent, 5)
            os.environ["ZILONG_MAX_CHAT_UPLOADS"] = "not-a-number"
            self.assertEqual(ChatUploadLimiter().max_concurrent, 3)  # falls back
            os.environ["ZILONG_MAX_CHAT_UPLOADS"] = "0"
            self.assertEqual(ChatUploadLimiter().max_concurrent, 1)  # clamped >=1
        finally:
            if prev is None:
                os.environ.pop("ZILONG_MAX_CHAT_UPLOADS", None)
            else:
                os.environ["ZILONG_MAX_CHAT_UPLOADS"] = prev

    async def test_explicit_max_default_is_three(self):
        prev = os.environ.get("ZILONG_MAX_CHAT_UPLOADS")
        try:
            os.environ.pop("ZILONG_MAX_CHAT_UPLOADS", None)
            self.assertEqual(ChatUploadLimiter().max_concurrent, 3)
        finally:
            if prev is not None:
                os.environ["ZILONG_MAX_CHAT_UPLOADS"] = prev


if __name__ == "__main__":
    unittest.main()
