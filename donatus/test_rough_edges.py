"""
Behavioural tests for the log-noise and API-mismatch fixes.

Covers three things that showed up in production logs:
  * /compress's text receiver ran on every private message and logged twice
    before ignoring it (magnets included).
  * The CloudConvert webhook manager listed on an endpoint that answers 405
    and warned about 404s from an idempotent delete.
  * Seedr rejected tracker-heavy magnets with 413 Payload Too Large.
"""
import os
import unittest

# plugins/resize.py imports core.config, which sys.exit(1)s without real bot
# credentials. Only the message filter is under test here, so supply throwaway
# values before the import rather than reproducing the whole module.
os.environ.setdefault("API_ID", "12345")
os.environ.setdefault("API_HASH", "test_hash")
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "1")

from services import seedr  # noqa: E402


class _Msg:
    def __init__(self, text, uid=42):
        self.text = text
        self.from_user = type("U", (), {"id": uid})()


class CompressFilterTests(unittest.IsolatedAsyncioTestCase):
    """The handler must not run for traffic it cannot act on."""

    def setUp(self):
        from plugins import resize
        self.resize = resize
        self.resize._compress_waiting.clear()

    def tearDown(self):
        self.resize._compress_waiting.clear()

    async def _matches(self, text, uid=42):
        return await self.resize._wants_compress_size(None, None, _Msg(text, uid))

    async def test_magnet_does_not_trigger_the_handler(self):
        """The reported noise: every magnet logged two INFO lines here."""
        magnet = ("magnet:?xt=urn:btih:ABCDEF0123456789&dn=Some+Show"
                  "&tr=udp%3A%2F%2Ftracker.example.org%3A6969")
        self.assertFalse(await self._matches(magnet))

    async def test_urls_and_prose_do_not_trigger_the_handler(self):
        for text in ("https://example.com/video.mkv", "hello", "/unknowncmd",
                     "Some Anime - 01 [1080p]"):
            self.assertFalse(await self._matches(text), text)

    async def test_waiting_user_matches_any_text(self):
        """While waiting, non-numeric text must still reach the handler so it
        can reply 'please send a number'."""
        self.resize._compress_waiting[42] = {"fname": "x.mkv"}
        self.assertTrue(await self._matches("not a number"))
        self.assertTrue(await self._matches("80"))

    async def test_numeric_text_matches_even_when_state_was_lost(self):
        """Preserves the restart-recovery path: state is gone but a bare
        number is still plausibly a compress reply."""
        self.assertTrue(await self._matches("80"))
        self.assertTrue(await self._matches("199.5"))

    async def test_other_users_are_unaffected_by_a_waiting_user(self):
        self.resize._compress_waiting[42] = {"fname": "x.mkv"}
        self.assertFalse(await self._matches("some text", uid=99))

    async def test_missing_text_or_user_is_ignored(self):
        class _Bare:
            text = None
            from_user = None
        self.assertFalse(
            await self.resize._wants_compress_size(None, None, _Bare()))


class MagnetCompactionTests(unittest.TestCase):
    """Seedr 413 Payload Too Large on tracker-heavy magnets."""

    def _magnet(self, trackers):
        return ("magnet:?xt=urn:btih:ABC123&dn=Some+Show+-+01+%5B1080p%5D"
                + "".join(f"&tr=udp%3A%2F%2Ftracker{i}.example.org%3A6969"
                          f"%2Fannounce" for i in range(trackers)))

    def test_surplus_trackers_are_dropped(self):
        original = self._magnet(40)
        compact = seedr.compact_magnet(original)

        self.assertLess(len(compact), len(original) // 2)
        self.assertEqual(compact.count("&tr="), seedr._MAGNET_MAX_TRACKERS)

    def test_info_hash_and_name_survive_compaction(self):
        """Trimming must not change which torrent the magnet identifies."""
        compact = seedr.compact_magnet(self._magnet(40))
        self.assertIn("xt=urn:btih:ABC123", compact)
        self.assertIn("dn=Some+Show", compact)

    def test_non_tracker_parameters_are_preserved(self):
        magnet = "magnet:?xt=urn:btih:ABC&dn=X&xl=12345&tr=a&tr=b&tr=c&tr=d&tr=e&tr=f"
        compact = seedr.compact_magnet(magnet)
        self.assertIn("xl=12345", compact)

    def test_magnets_within_limits_are_untouched(self):
        for magnet in ("magnet:?xt=urn:btih:ABC",
                       "magnet:?xt=urn:btih:ABC&dn=X&tr=a&tr=b",
                       ""):
            self.assertEqual(seedr.compact_magnet(magnet), magnet)

    def test_minimal_magnet_keeps_only_the_info_hash(self):
        bare = seedr.minimal_magnet(self._magnet(40))
        self.assertEqual(bare, "magnet:?xt=urn:btih:ABC123")

    def test_minimal_magnet_leaves_unparseable_input_alone(self):
        self.assertEqual(seedr.minimal_magnet("not-a-magnet"), "not-a-magnet")
        self.assertEqual(seedr.minimal_magnet("magnet:?dn=only"), "magnet:?dn=only")

    def test_payload_too_large_detection(self):
        for text in ("HTTP 413", "Payload Too Large", "request entity too large"):
            self.assertTrue(seedr._is_payload_too_large(Exception(text)), text)
        for text in ("timeout", "401 Unauthorized", ""):
            self.assertFalse(seedr._is_payload_too_large(Exception(text)), text)


class CCWebhookApiTests(unittest.IsolatedAsyncioTestCase):
    """CloudConvert webhook endpoint mismatches seen in the logs."""

    def setUp(self):
        from services import cc_webhook_mgr
        self.mgr = cc_webhook_mgr
        self.mgr._list_path_cache = None

    def tearDown(self):
        self.mgr._list_path_cache = None

    def test_listing_prefers_the_users_me_collection(self):
        """GET /v2/webhooks answers 405; the collection lives under users/me."""
        self.assertEqual(self.mgr._LIST_PATHS[0], "/users/me/webhooks")
        self.assertIn("/webhooks", self.mgr._LIST_PATHS)

    async def test_delete_treats_404_as_success(self):
        """A stored id from a previous run is already gone. Deletion is
        idempotent, so 404 is the desired end state, not a warning."""
        import types

        class _Resp:
            status = 404
            async def text(self): return "not found"
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        class _Sess:
            def delete(self, *a, **k): return _Resp()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        saved = self.mgr.aiohttp
        self.mgr.aiohttp = types.SimpleNamespace(
            ClientSession=lambda *a, **k: _Sess(),
            ClientTimeout=saved.ClientTimeout,
        )
        try:
            self.assertTrue(await self.mgr.delete_cc_webhook("key", "wh_123"))
        finally:
            self.mgr.aiohttp = saved


if __name__ == "__main__":
    unittest.main()
