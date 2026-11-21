import os
import unittest

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("OWNER_ID", "1")

from plugins.start import _settings_kb, _settings_text


SAMPLE = {
    "upload_mode": "auto",
    "progress_style": "A",
    "progress_frequency": "normal",
    "hardsub_engine": "fc",
    "cc_engine_mode": "balanced",
    "seedr_cleanup": "success",
    "quality_profile": "balanced",
    "custom_name_mode": "mid",
    "auto_forward": True,
    "nyaa_quality": "1080p",
    "nyaa_min_seeders": 0,
}


def labels(kb):
    return [[button.text for button in row] for row in kb.inline_keyboard]


class SettingsMobileTests(unittest.TestCase):
    def test_main_text_compact(self):
        text = _settings_text(SAMPLE, "BOT", "main")
        self.assertIn("Choose a category to configure.", text)
        self.assertIn("<code>Auto</code> · <code>Compact</code> · <code>Normal</code>", text)
        self.assertLessEqual(len(text.splitlines()), 5)
        self.assertNotIn("Auto upload · Compact panel · Balanced quality", text)

    def test_main_keyboard_category_buttons_only(self):
        self.assertEqual(labels(_settings_kb(SAMPLE, "main")), [
            ["📤 Upload", "🎨 Panel"],
            ["🔥 Engines", "🎚 Quality"],
            ["📝 Naming", "🖼 Thumb"],
            ["📡 Delivery", "🧭 Sources"],
            ["🧹 Reset", "✖ Close"],
        ])

    def test_main_keyboard_reject_old_value_labels(self):
        flat = " ".join(button for row in labels(_settings_kb(SAMPLE, "main")) for button in row)
        for bad in ["Upload:", "Panel:", "Freq:", "Quality:", "Hardsub:", "CC:", "Balanced CPU", "FreeConvert", "CloudConvert", "..."]:
            self.assertNotIn(bad, flat)
        for row in labels(_settings_kb(SAMPLE, "main")):
            for button in row:
                self.assertLessEqual(len(button), 12 if button != "✖ Close" else 24)

    def test_sections_have_values_in_body_not_buttons(self):
        engines_text = _settings_text(SAMPLE, "BOT", "engines")
        self.assertIn("FreeConvert", engines_text)
        self.assertIn("Balanced CPU", engines_text)
        engine_buttons = " ".join(button for row in labels(_settings_kb(SAMPLE, "engines")) for button in row)
        self.assertNotIn("FreeConvert", engine_buttons)
        self.assertNotIn("Balanced CPU", engine_buttons)
        self.assertIn("Style: <code>Compact</code>", _settings_text(SAMPLE, "BOT", "panel"))
        self.assertIn("Frequency: <code>Normal</code>", _settings_text(SAMPLE, "BOT", "panel"))

    def test_sections_have_back_close(self):
        for section in ["upload", "panel", "engines", "quality", "naming", "thumb", "delivery", "sources"]:
            self.assertEqual(labels(_settings_kb(SAMPLE, section))[-1], ["🔙 Back", "✖ Close"])

    def test_callback_data_stable(self):
        data = [[button.callback_data for button in row] for row in _settings_kb(SAMPLE, "main").inline_keyboard]
        self.assertEqual(data[0], ["st_nav|upload", "st_nav|panel"])
        self.assertEqual(data[1], ["st_nav|engines", "st_nav|quality"])
        self.assertEqual(data[4], ["st_reset", "st_close"])
        for section, expected in {
            "upload": "st_mode",
            "panel": "st_progress_style",
            "engines": "st_hs_engine",
            "quality": "st_quality_profile",
            "naming": "st_cn_cycle",
            "thumb": "st_thumb",
            "delivery": "st_af_toggle",
            "sources": "st_nyaa_quality",
        }.items():
            callbacks = [button.callback_data for row in _settings_kb(SAMPLE, section).inline_keyboard for button in row]
            self.assertIn(expected, callbacks)


if __name__ == '__main__':
    unittest.main()
