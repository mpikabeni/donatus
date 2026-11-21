import unittest
from pathlib import Path

from services.ui import (
    panel_footer, render_error_panel, render_status_panel, truncate_middle,
)

class UITests(unittest.TestCase):
    def test_error_panel_escapes(self):
        t = render_error_panel('Err', '<boom>', 'Use <safe>')
        self.assertIn('&lt;boom&gt;', t)

    def test_truncate_middle(self):
        s = truncate_middle('a'*100, 20)
        self.assertLessEqual(len(s), 21)
        self.assertIn('…', s)

    def test_footer_grammar(self):
        self.assertIn('Time is precious', panel_footer())

    def test_status_panel_is_compact_polished_and_escaped(self):
        text = render_status_panel(
            "Media analysis", "Reading streams", icon="🔎",
            file="episode <15>.mkv", size="104.82 MiB",
            elapsed=15, limit=45,
            detail="Updates appear here automatically.",
        )
        self.assertIn("🔎 <b>Media analysis</b>", text)
        self.assertIn("episode &lt;15&gt;.mkv", text)
        self.assertIn("104.82 MiB", text)
        self.assertIn("15s / 45s", text)
        self.assertIn("▰▰▰▰", text)
        self.assertLessEqual(len(text.splitlines()), 10)

    def test_major_workflows_do_not_restore_bare_wait_messages(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                Path("plugins/url_handler.py"),
                Path("plugins/stream_extractor.py"),
                Path("plugins/video.py"),
                Path("plugins/archive.py"),
                Path("plugins/hardsub.py"),
            )
        )
        for legacy in (
            '"⬇️ Downloading…"',
            '"📡 Fetching stream list…"',
            '"📤 Extracting stream…"',
            '"🔍 Analysing streams…"',
            '"📂 Extracting…"',
        ):
            self.assertNotIn(legacy, source)

if __name__ == '__main__':
    unittest.main()
