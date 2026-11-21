"""
tests/test_upload_concurrency_wiring.py

Structural tests asserting that the upload-concurrency fix is wired into the
canonical uploader (not a runtime monkey patch). These read source text so they
run without pyrogram installed.

Requirements covered:
  * services/uploader.py uses the bounded per-chat limiter (no asyncio.Lock gate).
  * The slot wraps only the Telegram send stage; log-channel forwarding and
    auto-forward run as a background task AFTER the slot is released.
  * The progress panel is deleted (or marked complete) after a successful send.
  * plugins/zz_upload_lock_fix.py no longer patches services.uploader and
    exposes /debuguploads + /unlockuploads admin commands.
  * Public imports `upload_file` and `TG_MAX_BYTES` are preserved.
"""
import unittest
from pathlib import Path

UPLOADER = Path("services/uploader.py").read_text(encoding="utf-8")
PLUGIN = Path("plugins/zz_upload_lock_fix.py").read_text(encoding="utf-8")


class UploaderWiringTests(unittest.TestCase):
    def test_uses_bounded_limiter_not_lock(self):
        self.assertIn("from services.upload_limiter import chat_upload_limiter", UPLOADER)
        self.assertIn("chat_upload_limiter.will_queue(chat_id)", UPLOADER)
        self.assertIn("async with chat_upload_limiter.slot(chat_id):", UPLOADER)
        # The old single per-chat lock gate is gone.
        self.assertNotIn("_UPLOAD_LOCKS", UPLOADER)
        self.assertNotIn("def _upload_lock", UPLOADER)
        self.assertNotIn("Upload queued", UPLOADER)

    def test_soft_waiting_message_present(self):
        self.assertIn("Upload waiting", UPLOADER)
        self.assertIn("I’ll start this one automatically", UPLOADER)

    def test_public_symbols_preserved(self):
        self.assertIn("TG_MAX_BYTES = ", UPLOADER)
        self.assertIn("async def upload_file(", UPLOADER)
        self.assertIn("async def _upload_single(", UPLOADER)

    def test_post_upload_work_runs_outside_the_slot(self):
        slot_idx = UPLOADER.index("async with chat_upload_limiter.slot(chat_id):")
        released_idx = UPLOADER.index("Upload slot released")
        self.assertLess(slot_idx, released_idx)

        slot_body = UPLOADER[slot_idx:released_idx]
        # Forwarding / auto-forward must NOT happen while the slot is held.
        self.assertNotIn("_run_post_upload_hooks", slot_body)
        self.assertNotIn("sent.forward", slot_body)
        self.assertNotIn(".copy(", slot_body)

        after_slot = UPLOADER[released_idx:]
        # Post-upload hooks are scheduled via the safe background-task helper.
        self.assertIn("spawn(_run_post_upload_hooks(", after_slot)

    def test_post_upload_hooks_are_bounded(self):
        self.assertIn("async def _run_post_upload_hooks(", UPLOADER)
        # Log-channel forwarding moved behind services/log_channel.py, which
        # keeps the timeout and adds a breaker for permanently bad channel IDs.
        self.assertIn("_log_channel.forward(sent, cfg.log_channel)", UPLOADER)
        self.assertIn("asyncio.wait_for(sent.copy(ch_id)", UPLOADER)

    def test_panel_deleted_or_marked_complete(self):
        self.assertIn("async def _delete_or_mark_complete(", UPLOADER)
        self.assertIn("Upload complete", UPLOADER)
        self.assertIn("await _delete_or_mark_complete(status_msg, fname)", UPLOADER)
        # Deletion/edit are bounded so a stuck panel cannot block uploads.
        self.assertIn("asyncio.wait_for(safe_delete_message(status_msg), timeout=", UPLOADER)


class PluginWiringTests(unittest.TestCase):
    def test_no_runtime_monkey_patch(self):
        self.assertNotIn("_patched_upload_single", PLUGIN)
        self.assertNotIn("up._upload_single", PLUGIN)
        self.assertNotIn("_ZILONG_UPLOAD_LOCK_FIX_PATCHED", PLUGIN)
        self.assertNotIn("def install", PLUGIN)

    def test_admin_commands_present(self):
        self.assertIn("from services.upload_limiter import chat_upload_limiter", PLUGIN)
        self.assertIn('filters.command("debuguploads")', PLUGIN)
        self.assertIn("unlockuploads", PLUGIN)
        self.assertIn("chat_upload_limiter.snapshot()", PLUGIN)
        self.assertIn("chat_upload_limiter.reset(", PLUGIN)
        # Admin-gated.
        self.assertIn("cfg.admins", PLUGIN)


if __name__ == "__main__":
    unittest.main()
