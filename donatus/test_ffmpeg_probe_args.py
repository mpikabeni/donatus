from __future__ import annotations

import unittest

from services import ffmpeg as FF


class ProbeBaseArgsTests(unittest.TestCase):
    """URL probes must use light ffprobe limits so they answer within the
    10-25s probe timeouts; the 50 MB local probesize forced ffprobe to pull
    up to 50 MB over HTTP first, timing out every pass on slow CDNs."""

    def test_urls_get_light_probe_limits(self):
        base = FF._probe_base_args([
            "-show_streams", "-show_format", "https://cdn.seedr.cc/file.mkv",
        ])
        self.assertIs(base, FF._FPROBE_URL)
        self.assertIn("5000000", base)
        self.assertNotIn("50000000", base)

    def test_local_files_keep_full_probe_limits(self):
        base = FF._probe_base_args([
            "-show_streams", "-show_format", r"C:\downloads\file.mkv",
        ])
        self.assertIs(base, FF._FPROBE)
        self.assertIn("50000000", base)

    def test_retry_pass_overrides_still_win_for_urls(self):
        # _probe_streams_uncached pass-2 appends bigger explicit limits after
        # the base; ffprobe applies the last occurrence, so the retry keeps
        # its deep scan even for URLs.
        args = [
            "-analyzeduration", "60000000", "-probesize", "100000000",
            "-show_streams", "-show_format", "https://cdn.seedr.cc/file.mkv",
        ]
        cmd = FF._probe_base_args(args) + args
        self.assertGreater(
            cmd.index("100000000"),
            cmd.index("5000000"),
        )


if __name__ == "__main__":
    unittest.main()
