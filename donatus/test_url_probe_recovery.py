from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("API_ID", "12345")
os.environ.setdefault("API_HASH", "test_hash")
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "1")

try:
    import yt_dlp  # noqa: F401
except ImportError:
    # downloader imports yt-dlp at module load. These unit tests mock every
    # extractor call, so a tiny import-only stand-in keeps local no-deps runs
    # useful without masking a real installed package.
    ytdlp_stub = types.ModuleType("yt_dlp")
    ytdlp_stub.YoutubeDL = object
    sys.modules["yt_dlp"] = ytdlp_stub

try:
    import aria2p  # noqa: F401
except ImportError:
    aria2p_stub = types.ModuleType("aria2p")
    aria2p_stub.API = object
    aria2p_stub.Client = object
    sys.modules["aria2p"] = aria2p_stub

from services import downloader


class _Content:
    def __init__(self, body: bytes):
        self._body = body

    async def iter_chunked(self, _size: int):
        yield self._body


class _Response:
    status = 200

    def __init__(self, body: bytes, content_type: str):
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        self.content = _Content(body)

    def raise_for_status(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    def __init__(self, response: _Response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        return self.response


class DirectDownloadValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_declared_html_before_creating_media_file(self):
        response = _Response(b"<html>player</html>", "text/html; charset=utf-8")
        with tempfile.TemporaryDirectory() as dest, patch.object(
            downloader, "_get_session", return_value=_Session(response),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTML web page"):
                await downloader.download_direct("https://example.test/watch/1", dest)
            self.assertEqual(os.listdir(dest), [])

    async def test_rejects_html_body_mislabeled_as_binary(self):
        response = _Response(b"  <!doctype html><video></video>", "application/octet-stream")
        with tempfile.TemporaryDirectory() as dest, patch.object(
            downloader, "_get_session", return_value=_Session(response),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTML web page"):
                await downloader.download_direct("https://example.test/watch/2", dest)
            self.assertEqual(os.listdir(dest), [])


class StreamExtractorFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_site_uses_ytdlp_after_direct_html_response(self):
        from plugins import stream_extractor as stream

        info = {
            "title": "Resolved player",
            "formats": [{
                "format_id": "18", "height": 360, "width": 640,
                "vcodec": "h264", "acodec": "aac", "ext": "mp4",
            }],
        }
        status = AsyncMock()
        status.edit = AsyncMock(return_value=status)

        with (
            patch("services.downloader.download_direct", AsyncMock(
                side_effect=RuntimeError("URL returned an HTML web page instead of a media file")
            )),
            patch.object(stream, "extract_ytdlp_info", AsyncMock(return_value=info)),
            patch.object(stream, "_show_url_streams", AsyncMock()) as show,
            patch.object(stream, "cleanup"),
            patch.object(stream, "make_tmp", return_value="probe-temp"),
        ):
            await stream.extract_url_streams(
                AsyncMock(), status, "https://unknown-video.example/watch/abc", 42,
                edit=False,
            )

        show.assert_awaited_once_with(
            unittest.mock.ANY, status, "https://unknown-video.example/watch/abc", info, 42,
        )


class MagnetProbeUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_subtitle_buttons_show_flags_and_languages_in_two_columns(self):
        from plugins import url_handler

        streams = {
            "video": [], "audio": [],
            "subtitle": [
                {"index": 2, "codec_name": "ass", "tags": {"language": "eng"}},
                {"index": 3, "codec_name": "ass", "tags": {"language": "spa"}},
                {"index": 4, "codec_name": "subrip", "tags": {"language": "ara"}},
            ],
        }
        with patch.object(url_handler, "important_edit", new=AsyncMock()) as edit:
            await url_handler._show_magnet_streams(
                AsyncMock(), AsyncMock(), "session", streams, 60,
                "episode.mkv", 42,
            )

        keyboard = edit.await_args.kwargs["reply_markup"].inline_keyboard
        subtitle_rows = keyboard[:2]
        self.assertEqual([len(row) for row in subtitle_rows], [2, 1])
        labels = [button.text for row in subtitle_rows for button in row]
        self.assertTrue(any("🇬🇧 English" in label for label in labels))
        self.assertTrue(any("🇪🇸 Spanish" in label for label in labels))
        self.assertTrue(any("🇸🇦 Arabic" in label for label in labels))
        self.assertFalse(any("Sub 2" in label for label in labels))

    async def test_stream_menu_falls_back_to_fresh_message_when_edit_is_dropped(self):
        from plugins import url_handler

        streams = {
            "video": [{
                "index": 0, "codec_name": "h264", "width": 1280,
                "height": 720, "r_frame_rate": "24/1",
            }],
            "audio": [], "subtitle": [],
        }
        client = SimpleNamespace(send_message=AsyncMock(return_value="fresh"))
        status = SimpleNamespace(delete=AsyncMock())
        with patch.object(
            url_handler, "important_edit", new=AsyncMock(return_value=False),
        ):
            result = await url_handler._show_magnet_streams(
                client, status, "session", streams, 60,
                "episode <15>.mkv", 42,
            )

        self.assertEqual(result, "fresh")
        client.send_message.assert_awaited_once()
        sent = client.send_message.await_args.args[1]
        self.assertIn("episode &lt;15&gt;.mkv", sent)
        status.delete.assert_awaited_once()

    async def test_mediainfo_summary_is_published_when_full_report_fails(self):
        from plugins import url_handler
        from services import ffmpeg

        fd, path = tempfile.mkstemp(suffix=".mkv")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path, "wb") as fh:
            fh.write(b"media")
        tmp = os.path.dirname(path)
        probe = {
            "streams": {
                "video": [{"codec_name": "h264", "width": 1280, "height": 720}],
                "audio": [], "subtitle": [],
            },
            "duration": 60,
            "fname": "episode.mkv",
        }
        status = AsyncMock()
        message = SimpleNamespace(edit=AsyncMock(return_value=status))
        cb = SimpleNamespace(message=message, from_user=SimpleNamespace(id=42))

        with (
            patch.object(url_handler, "_probe_magnet_file", new=AsyncMock(
                return_value=(path, tmp, probe),
            )),
            patch.object(url_handler, "important_edit", new=AsyncMock()) as edit,
            patch.object(url_handler, "cleanup") as clean,
            patch.object(ffmpeg, "get_mediainfo", new=AsyncMock(
                side_effect=RuntimeError("full report unavailable"),
            )),
        ):
            await url_handler._handle_magnet_info(
                AsyncMock(), cb, "magnet:?xt=urn:btih:test", "token",
            )

        edit.assert_awaited_once()
        self.assertIn("Magnet Media Info", edit.await_args.args[1])
        clean.assert_called_once_with(tmp)


if __name__ == "__main__":
    unittest.main()
