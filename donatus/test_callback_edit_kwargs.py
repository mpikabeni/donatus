import unittest
from pyrogram import enums

from services.utils import safe_callback_edit

class _Chat:
    id = 1

class _Msg:
    def __init__(self):
        self.chat = _Chat()
        self.id = 10
        self.calls = []
    async def edit(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return self


class _NotModifiedMsg(_Msg):
    def __init__(self):
        super().__init__()
        self.id = 987654
    async def edit(self, text, **kwargs):
        self.calls.append((text, kwargs))
        raise Exception("MESSAGE_NOT_MODIFIED")

class _Cb:
    def __init__(self):
        self.message = _Msg()
        self.data = 'dl|menu|tok|process'
        self.answers=[]
    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

class CallbackEditKwargsTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_callback_edit_no_duplicate_kwargs(self):
        cb = _Cb()
        ok = await safe_callback_edit(cb, 'hello', parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True)
        self.assertTrue(ok)
        self.assertEqual(len(cb.message.calls), 1)
        _, kw = cb.message.calls[0]
        self.assertIn('parse_mode', kw)
        self.assertIn('disable_web_page_preview', kw)

    async def test_safe_callback_edit_treats_not_modified_as_success(self):
        cb = _Cb()
        cb.message = _NotModifiedMsg()
        ok = await safe_callback_edit(cb, 'same text', parse_mode=enums.ParseMode.HTML)
        self.assertTrue(ok)
        self.assertEqual(cb.answers, [])
        self.assertEqual(len(cb.message.calls), 1)

if __name__ == '__main__':
    unittest.main()
