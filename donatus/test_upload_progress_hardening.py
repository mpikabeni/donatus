import re
import unittest
from pathlib import Path

from services.utils import progress_panel


class UploadProgressHardeningTests(unittest.TestCase):
    def test_upload_progress_throttling_is_present(self):
        src = Path("services/uploader.py").read_text(encoding="utf-8")
        self.assertIn("def _upload_progress_step", src)
        self.assertIn("_UPLOAD_PROGRESS_MIN_SECONDS", src)
        self.assertIn("current - last_progress_tick", src)
        self.assertIn("progress_step = _upload_progress_step(file_size)", src)
        self.assertRegex(src, re.compile(r"return\s+4 \* 1024 \* 1024"))
        self.assertRegex(src, re.compile(r"return\s+8 \* 1024 \* 1024"))

    def test_uploader_uses_policy_and_safe_initial_edit(self):
        src = Path("services/uploader.py").read_text(encoding="utf-8")
        self.assertIn("progress_update_policy(", src)
        self.assertIn("await safe_edit_message(\n            msg,\n            initial_panel", src)
        self.assertNotIn("await msg.edit(initial_panel", src)

    def test_progress_footer_is_clean_grammar(self):
        panel = progress_panel(mode="ul", fname="file.mkv", done=1, total=2, style="B")
        self.assertIn("Time is precious", panel)
        self.assertNotIn("When I'm Doin", panel)


if __name__ == "__main__":
    unittest.main()
