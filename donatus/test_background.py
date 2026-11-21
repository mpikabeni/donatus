"""
tests/test_background.py

Unit tests for the safe fire-and-forget task helper (services/background.py).
Dependency-free — runs without pyrogram.
"""
import asyncio
import logging
import unittest

from services import background
from services.background import spawn, active_count


class SpawnTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_the_coroutine(self):
        done = asyncio.Event()

        async def work():
            done.set()

        task = spawn(work())
        await asyncio.wait_for(done.wait(), timeout=1)
        await task
        self.assertTrue(task.done())

    async def test_keeps_reference_while_running_then_releases(self):
        release = asyncio.Event()

        async def work():
            await release.wait()

        before = active_count()
        task = spawn(work())
        await asyncio.sleep(0)  # let it start
        self.assertEqual(active_count(), before + 1)  # referenced while running

        release.set()
        await task
        await asyncio.sleep(0)  # let the done-callback run
        self.assertEqual(active_count(), before)  # released after completion

    async def test_exception_is_logged_not_raised_to_caller(self):
        async def boom():
            raise ValueError("kaboom")

        # spawn() must not propagate the error to the caller...
        with self.assertLogs(background.log, level=logging.ERROR) as cm:
            task = spawn(boom())
            # ...but the error must be logged (not silently swallowed).
            with self.assertRaises(ValueError):
                await task
            await asyncio.sleep(0)  # allow done-callback to log
        self.assertTrue(any("kaboom" in line for line in cm.output))
        self.assertEqual(active_count(), 0)

    async def test_cancelled_task_does_not_error(self):
        async def work():
            await asyncio.sleep(10)

        task = spawn(work())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)  # done-callback runs; must not raise
        self.assertEqual(active_count(), 0)

    async def test_name_is_passed_through(self):
        async def work():
            return None

        task = spawn(work(), name="my-task")
        self.assertEqual(task.get_name(), "my-task")
        await task


if __name__ == "__main__":
    unittest.main()
