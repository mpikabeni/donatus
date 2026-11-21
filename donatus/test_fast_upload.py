from __future__ import annotations

import inspect
import os
import unittest
from unittest import mock

from plugins import zz_fast_upload


class FastUploadPatchTests(unittest.TestCase):
    def test_patch_installs_on_pinned_pyrofork(self):
        """The signature guard must accept the pinned pyrofork layout and the
        patched save_file must be in place of the stock one (which starves
        its 4 workers behind asyncio.Queue(1))."""
        self.assertTrue(zz_fast_upload.INSTALLED)
        from pyrogram.client import Client

        self.assertEqual(
            list(inspect.signature(Client.save_file).parameters),
            ["self", "path", "file_id", "file_part", "progress", "progress_args"],
        )
        source = inspect.getsource(Client.save_file)
        self.assertIn("workers_count * 2", source)
        self.assertIn("upload_sessions()", source)
        self.assertNotIn("asyncio.Queue(1)", source)

    def test_worker_and_session_defaults_and_clamps(self):
        with mock.patch.dict(os.environ, {"TG_UPLOAD_WORKERS": "", "TG_UPLOAD_SESSIONS": ""}):
            self.assertEqual(zz_fast_upload.upload_workers(), 12)
            self.assertEqual(zz_fast_upload.upload_sessions(), 3)
        with mock.patch.dict(os.environ, {"TG_UPLOAD_WORKERS": "99", "TG_UPLOAD_SESSIONS": "99"}):
            self.assertEqual(zz_fast_upload.upload_workers(), 16)
            self.assertEqual(zz_fast_upload.upload_sessions(), 4)
        with mock.patch.dict(os.environ, {"TG_UPLOAD_WORKERS": "1", "TG_UPLOAD_SESSIONS": "0"}):
            self.assertEqual(zz_fast_upload.upload_workers(), 2)
            self.assertEqual(zz_fast_upload.upload_sessions(), 1)
        with mock.patch.dict(os.environ, {"TG_UPLOAD_WORKERS": "garbage"}):
            self.assertEqual(zz_fast_upload.upload_workers(), 12)


if __name__ == "__main__":
    unittest.main()
