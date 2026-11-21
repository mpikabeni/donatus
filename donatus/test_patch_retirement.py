"""
tests/test_patch_retirement.py

Guards the Tier-2 #4 work: two runtime monkey-patches were retired by folding
their behaviour into canonical code. These are source/text checks so they run
without pyrogram.
"""
import unittest
from pathlib import Path


class PatchRetirementTests(unittest.TestCase):
    def test_smart_thumbnail_patch_is_gone(self):
        self.assertFalse(Path("plugins/smart_thumbnail_patch.py").exists())
        # main.py no longer preloads the deleted patch.
        main_src = Path("main.py").read_text(encoding="utf-8")
        self.assertNotIn("smart_thumbnail_patch", main_src)

    def test_smart_thumbnail_is_folded_into_uploader(self):
        src = Path("services/uploader.py").read_text(encoding="utf-8")
        # The canonical uploader now builds a normal-photo HD cover separately
        # from Telegram's small legacy document thumbnail.
        self.assertIn(
            "from services.smart_thumbnail import make_smart_preview_assets", src
        )
        self.assertIn("async def _make_preview(", src)
        self.assertIn("make_smart_preview_assets(path, duration)", src)
        self.assertIn("timeout=_SMART_THUMB_TIMEOUT_SECONDS", src)
        self.assertIn("cover              = cover", src)

    def test_url_download_cache_fix_is_canonical(self):
        # zz_url_download_fix only existed to create a missing _cache; it is now
        # defined directly in url_handler, so the patch is deleted.
        self.assertFalse(Path("plugins/zz_url_download_fix.py").exists())
        uh = Path("plugins/url_handler.py").read_text(encoding="utf-8")
        self.assertIn("_cache", uh)
        self.assertRegex(uh, r"_cache\s*:\s*dict")


if __name__ == "__main__":
    unittest.main()
