import asyncio
import os
import unittest

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("OWNER_ID", "1")

from core.session import sessions
from services.cancel_manager import cancel_all_for_user, register_user_task, unregister_user_task


class CancelCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        # Keep tests from leaking fake sessions into persisted state.
        for key in list(sessions._data):
            if key.startswith("999001_"):
                await sessions.remove(key)

    async def test_cancel_registered_task_is_force_cancelled(self):
        uid = 999001
        task = asyncio.create_task(asyncio.sleep(60))
        register_user_task(uid, "test sleeper", task)
        summary = await cancel_all_for_user(uid)
        await asyncio.sleep(0)
        self.assertGreaterEqual(summary.registered_tasks, 1)
        self.assertTrue(task.cancelled())
        unregister_user_task(uid, task)

    async def test_cancel_clears_waiting_file_sessions(self):
        uid = 999001
        session = await sessions.create(uid, "file-id", "video.mkv", 123, ".mkv", "/tmp")
        session.waiting = "video_merge_second"
        sessions.save()
        summary = await cancel_all_for_user(uid)
        self.assertGreaterEqual(summary.sessions, 1)
        self.assertIsNone(session.waiting)

    def test_cancel_plugin_uses_early_global_handler(self):
        with open("plugins/cancel.py", encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('filters.command("cancel")', src)
        self.assertIn("group=-90", src)
        self.assertIn("cancel_all_for_user", src)
        self.assertIn("stop_propagation", src)


if __name__ == "__main__":
    unittest.main()
