import unittest
from pathlib import Path

from services.url_tokens import URLTokenStore
from services import url_views

class URLCallbackTests(unittest.TestCase):
    def test_url_views_dl_format(self):
        kb = url_views.url_keyboard('tok123','magnet','main')
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn('dl|menu|tok123|download', data)
        self.assertIn('dl|menu|tok123|inspect', data)
        self.assertIn('dl|menu|tok123|process', data)
        self.assertIn('dl|cancel|tok123', data)

    def test_url_token_store(self):
        s = URLTokenStore()
        t = s.store('https://example.com/a')
        self.assertTrue(t)
        self.assertEqual(s.get(t), 'https://example.com/a')
        self.assertEqual(s.get('missing'), '')

    def test_cache_defined(self):
        src = Path('plugins/url_handler.py').read_text(encoding='utf-8')
        self.assertIn('_cache: dict[str, str] = {}', src)

    def test_process_menu_text(self):
        text = url_views.url_menu_text("magnet", "process", "[Erai] Demo.mkv")
        self.assertIn("Processing tools", text)
        self.assertIn("Recommended:", text)
        self.assertIn("Magnet / Torrent", text)

    def test_submenus_have_back_cancel(self):
        kb = url_views.url_keyboard('tok123', 'magnet', 'process')
        last = kb.inline_keyboard[-1]
        labels = [b.text for b in last]
        self.assertIn("🔙 Back", labels)
        self.assertIn("❌ Cancel", labels)


    def test_magnet_download_one_col_short_labels(self):
        kb = url_views.url_keyboard('tok123','magnet','download')
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn('🟢 Local', labels)
        self.assertIn('☁️ Seedr', labels)

    def test_magnet_process_compact_labels(self):
        kb = url_views.url_keyboard('tok123','magnet','process')
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn('⚡ Auto', labels)
        self.assertIn('🔥 FC', labels)
        self.assertIn('☁️ CC', labels)
        self.assertIn('🖥 Local', labels)

    def test_preview_truncated_phone_safe(self):
        text = url_views.url_menu_text('magnet','main','x'*200)
        self.assertIn('…', text)

    def test_submenu_line_count_reasonable(self):
        text = url_views.url_menu_text('magnet','download','demo')
        self.assertLessEqual(len(text.splitlines()), 12)

    def test_two_col_rows_mobile_safe(self):
        kb = url_views.url_keyboard('tok123','magnet','process')
        for row in kb.inline_keyboard:
            if len(row)==2:
                self.assertLessEqual(len(row[0].text)+len(row[1].text), 28)

    def test_handlers_exist(self):
        src = Path('plugins/url_handler.py').read_text(encoding='utf-8')
        self.assertIn('filters.regex(r"^dl\\|")', src)
        self.assertIn('filters.regex(r"^lcv\\|")', src)
        self.assertIn('filters.regex(r"^debugbtn\\|")', src)

if __name__ == '__main__':
    unittest.main()
