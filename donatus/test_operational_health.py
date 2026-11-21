"""
Behavioural tests for the operational-failure handling.

Each of these covers a failure that previously presented to users as "the bot
just stopped working", with nothing in the logs or panels pointing at the
cause: a permanently bad LOG_CHANNEL retried on every upload, a Drive mount
that silently stopped persisting, and FreeConvert keys out of CPU minutes that
routing kept selecting anyway.
"""
import os
import tempfile
import unittest
from pathlib import Path

from services import log_channel, persistent_paths


class _FakeSent:
    def __init__(self, exc=None):
        self.exc = exc
        self.calls = 0

    async def forward(self, chat_id):
        self.calls += 1
        if self.exc:
            raise self.exc
        return True


class LogChannelBreakerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        log_channel.reset()

    def tearDown(self):
        log_channel.reset()

    async def test_permanent_error_disables_forwarding_after_one_attempt(self):
        """CHANNEL_INVALID cannot recover, so the second upload must not retry.

        This is the actual reported symptom: every single upload paid for a
        forward that could never succeed.
        """
        sent = _FakeSent(Exception("CHANNEL_INVALID"))

        self.assertFalse(await log_channel.forward(sent, -100123))
        self.assertEqual(sent.calls, 1)
        self.assertTrue(log_channel.status()["disabled"])

        # Every later upload short-circuits without touching Telegram.
        for _ in range(5):
            self.assertFalse(await log_channel.forward(sent, -100123))
        self.assertEqual(sent.calls, 1, "forward retried a permanent failure")

    async def test_transient_error_keeps_forwarding_enabled(self):
        """A timeout or flood is not a reason to stop forwarding forever."""
        sent = _FakeSent(Exception("Request timed out"))

        await log_channel.forward(sent, -100123)
        await log_channel.forward(sent, -100123)

        self.assertEqual(sent.calls, 2)
        self.assertFalse(log_channel.status()["disabled"])

    async def test_successful_forward_is_counted(self):
        sent = _FakeSent()
        self.assertTrue(await log_channel.forward(sent, -100123))
        self.assertEqual(log_channel.status()["forwarded"], 1)

    async def test_zero_chat_id_is_a_no_op(self):
        sent = _FakeSent()
        self.assertFalse(await log_channel.forward(sent, 0))
        self.assertEqual(sent.calls, 0)

    def test_permanent_error_classification(self):
        for text in ("CHANNEL_INVALID", "PEER_ID_INVALID",
                     "CHAT_WRITE_FORBIDDEN", "CHAT_ADMIN_REQUIRED"):
            self.assertTrue(log_channel.is_permanent_error(Exception(text)), text)
        for text in ("FLOOD_WAIT_30", "timed out", "Connection reset"):
            self.assertFalse(log_channel.is_permanent_error(Exception(text)), text)


class PersistentPathTests(unittest.TestCase):
    def setUp(self):
        persistent_paths.reset_cache()
        self._saved = {k: os.environ.get(k) for k in ("BOT_DATA_DIR", "FC_CACHE_DIR")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        persistent_paths.reset_cache()

    def test_explicit_dir_is_used_and_reported_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["BOT_DATA_DIR"] = tmp
            self.assertEqual(persistent_paths.get_data_dir(), str(Path(tmp)))
            status = persistent_paths.data_dir_status()
            self.assertTrue(status["persistent"])
            self.assertEqual(status["source"], "BOT_DATA_DIR")

    def test_directory_is_resolved_once_per_process(self):
        """Re-resolving per call is what split state across two directories
        when the Drive mount appeared or vanished mid-run."""
        with tempfile.TemporaryDirectory() as first, \
                tempfile.TemporaryDirectory() as second:
            os.environ["BOT_DATA_DIR"] = first
            resolved = persistent_paths.get_data_dir()

            # Environment changes mid-run must not move the data directory.
            os.environ["BOT_DATA_DIR"] = second
            self.assertEqual(persistent_paths.get_data_dir(), resolved)
            self.assertTrue(persistent_paths.data_file("x.json").startswith(resolved))

    def test_unwritable_target_falls_back_and_warns(self):
        """exists() is not proof of writability — a broken mount passes it.

        A regular file standing where the directory should be reproduces that
        on every platform: the path resolves, mkdir fails.
        """
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("not a directory", encoding="utf-8")
            os.environ["BOT_DATA_DIR"] = str(blocker / "data")

            resolved = persistent_paths.get_data_dir()
            status = persistent_paths.data_dir_status()

            self.assertEqual(resolved,
                             str(persistent_paths.get_repo_root() / "data"))
            self.assertFalse(status["persistent"])
            self.assertTrue(status["warning"])

    def test_state_from_an_ephemeral_run_is_migrated_forward(self):
        """The data-loss path: a run wrote settings locally because Drive was
        down; the next run must not read an empty Drive and lose them."""
        local = persistent_paths.get_repo_root() / "data"
        local.mkdir(parents=True, exist_ok=True)
        marker = local / "sessions.json"
        existed = marker.exists()
        original = marker.read_text(encoding="utf-8") if existed else None
        marker.write_text('{"42": {"thumb_quality": "hd"}}', encoding="utf-8")

        try:
            with tempfile.TemporaryDirectory() as drive:
                os.environ["BOT_DATA_DIR"] = drive
                persistent_paths.get_data_dir()
                status = persistent_paths.data_dir_status()

                self.assertIn("sessions.json", status["migrated"])
                copied = Path(drive) / "sessions.json"
                self.assertTrue(copied.is_file())
                self.assertIn("thumb_quality", copied.read_text(encoding="utf-8"))
        finally:
            if original is None:
                marker.unlink(missing_ok=True)
            else:
                marker.write_text(original, encoding="utf-8")

    def test_migration_never_overwrites_existing_persistent_state(self):
        """Drive state is authoritative; a stale local copy must not clobber it."""
        local = persistent_paths.get_repo_root() / "data"
        local.mkdir(parents=True, exist_ok=True)
        marker = local / "sessions.json"
        existed = marker.exists()
        original = marker.read_text(encoding="utf-8") if existed else None
        marker.write_text("STALE", encoding="utf-8")

        try:
            with tempfile.TemporaryDirectory() as drive:
                (Path(drive) / "sessions.json").write_text("CURRENT", encoding="utf-8")
                os.environ["BOT_DATA_DIR"] = drive
                persistent_paths.get_data_dir()

                self.assertEqual(
                    (Path(drive) / "sessions.json").read_text(encoding="utf-8"),
                    "CURRENT")
        finally:
            if original is None:
                marker.unlink(missing_ok=True)
            else:
                marker.write_text(original, encoding="utf-8")


class FreeConvertKeyHealthTests(unittest.TestCase):
    def setUp(self):
        from services import freeconvert_api
        self.fc = freeconvert_api
        self._saved_env = {k: os.environ.get(k)
                           for k in ["FC_API_KEY"] + [f"FC_API_KEY_{i}" for i in range(2, 10)]}
        for k in self._saved_env:
            os.environ.pop(k, None)
        self._saved_cache = dict(self.fc._fc_exhausted)
        self.fc._fc_exhausted.clear()

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.fc._fc_exhausted.clear()
        self.fc._fc_exhausted.update(self._saved_cache)

    def test_all_keys_usable_when_nothing_is_exhausted(self):
        os.environ["FC_API_KEY"] = "keyAAAAAA,keyBBBBBB"
        health = self.fc.fc_key_health()
        self.assertEqual(health["total"], 2)
        self.assertEqual(health["usable"], 2)
        self.assertTrue(self.fc.has_usable_fc_key())

    def test_exhausted_keys_are_reported_and_excluded(self):
        import time
        os.environ["FC_API_KEY"] = "keyAAAAAA,keyBBBBBB"
        self.fc._fc_exhausted["keyAAAAAA"] = time.time()

        health = self.fc.fc_key_health()
        self.assertEqual(health["exhausted"], 1)
        self.assertEqual(health["usable"], 1)
        self.assertTrue(self.fc.has_usable_fc_key())

    def test_all_keys_exhausted_reports_unusable(self):
        """The reported situation: 9 of 11 keys spent, then the rest."""
        import time
        os.environ["FC_API_KEY"] = "keyAAAAAA,keyBBBBBB"
        now = time.time()
        self.fc._fc_exhausted["keyAAAAAA"] = now
        self.fc._fc_exhausted["keyBBBBBB"] = now

        self.assertFalse(self.fc.has_usable_fc_key())
        self.assertEqual(self.fc.fc_key_health()["usable"], 0)

    def test_expired_exhaustion_counts_as_usable_again(self):
        """FreeConvert quota resets, so the breaker must not latch forever."""
        import time
        os.environ["FC_API_KEY"] = "keyAAAAAA"
        self.fc._fc_exhausted["keyAAAAAA"] = time.time() - (self.fc._FC_EXHAUSTION_TTL + 60)
        self.assertTrue(self.fc.has_usable_fc_key())


class EngineRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Routing must react to quota, not merely to a key being configured."""

    def setUp(self):
        from services import engine_router, freeconvert_api
        self.router = engine_router
        self.fc = freeconvert_api
        self._saved = {k: os.environ.get(k) for k in
                       ("FC_API_KEY", "CC_API_KEY")}
        self._saved_cache = dict(self.fc._fc_exhausted)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.fc._fc_exhausted.clear()
        self.fc._fc_exhausted.update(self._saved_cache)

    async def _decide(self, pref="auto"):
        import core.session as session

        class _Settings:
            async def get(self, uid):
                return {"hardsub_engine": pref}

        saved = session.settings
        session.settings = _Settings()
        try:
            return await self.router.get_hardsub_decision(1)
        finally:
            session.settings = saved

    async def test_auto_prefers_freeconvert_when_quota_remains(self):
        os.environ["FC_API_KEY"] = "keyAAAAAA"
        os.environ["CC_API_KEY"] = "cckey"
        self.fc._fc_exhausted.clear()

        self.assertEqual((await self._decide()).engine, "fc")

    async def test_auto_falls_back_to_cc_when_all_fc_keys_are_spent(self):
        """Without this the bot kept picking FreeConvert and every job 402'd —
        the 'works once then nothing' report."""
        import time
        os.environ["FC_API_KEY"] = "keyAAAAAA"
        os.environ["CC_API_KEY"] = "cckey"
        self.fc._fc_exhausted["keyAAAAAA"] = time.time()

        decision = await self._decide()
        self.assertEqual(decision.engine, "cc")
        self.assertIn("CPU minutes", decision.reason)

    async def test_reason_distinguishes_missing_key_from_spent_quota(self):
        import time
        os.environ.pop("FC_API_KEY", None)
        os.environ["CC_API_KEY"] = "cckey"
        no_key = await self._decide()
        self.assertNotIn("CPU minutes", no_key.reason)

        os.environ["FC_API_KEY"] = "keyAAAAAA"
        self.fc._fc_exhausted["keyAAAAAA"] = time.time()
        spent = await self._decide()
        self.assertIn("CPU minutes", spent.reason)


if __name__ == "__main__":
    unittest.main()
