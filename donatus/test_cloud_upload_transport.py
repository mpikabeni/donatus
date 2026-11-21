import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

# A few API-unit tests install a deliberately tiny aiohttp stub. This test
# exercises the real multipart payload implementation, so restore aiohttp when
# discovery imported that stub first.
_aiohttp = sys.modules.get("aiohttp")
if _aiohttp is not None and not hasattr(_aiohttp, "payload"):
    sys.modules.pop("aiohttp", None)
    importlib.import_module("aiohttp")

from services.http_upload import (
    ProgressFilePayload, multipart_upload_form, post_multipart_file,
)
from services import http_upload
from services import freeconvert_api
from services import cloudconvert_api


class CloudUploadTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_standard_multipart_progress_does_not_block_socket_writes(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path, "wb") as fh:
            fh.write(b"video-payload" * 1000)

        response_finished = False
        progress_after_response = []

        class Writer:
            body = bytearray()

            async def write(self, chunk):
                self.body.extend(chunk)

        class Response:
            status = 204

            async def text(self):
                return ""

        class RequestContext:
            def __init__(self, form):
                self.form = form

            async def __aenter__(self):
                nonlocal response_finished
                payload = self.form()
                self.payload_size = payload.size
                self.writer = Writer()
                await payload.write(self.writer)
                response_finished = True
                return Response()

            async def __aexit__(self, *args):
                return False

        class Session:
            def post(self, _url, **kwargs):
                self.kwargs = kwargs
                self.request = RequestContext(kwargs["data"])
                return self.request

        async def progress(_done, _total):
            progress_after_response.append(response_finished)

        session = Session()
        status, body = await post_multipart_file(
            session, "https://upload.example", {"key": "value"},
            path, "video.mkv", progress,
        )

        self.assertEqual((status, body), (204, ""))
        self.assertTrue(progress_after_response)
        self.assertTrue(all(progress_after_response))
        self.assertGreater(session.request.payload_size, os.path.getsize(path))
        self.assertIn(b"video-payload", session.request.writer.body)
        self.assertIs(session.kwargs["expect100"], False)

    async def test_cloud_uploads_are_serialized(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path, "wb") as fh:
            fh.write(b"payload")

        active = 0
        max_active = 0

        class Response:
            status = 204

            async def text(self):
                return ""

        class RequestContext:
            async def __aenter__(self):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.03)
                return Response()

            async def __aexit__(self, *args):
                nonlocal active
                active -= 1
                return False

        class Session:
            def post(self, _url, **_kwargs):
                return RequestContext()

        await asyncio.gather(
            post_multipart_file(Session(), "https://one", {}, path, "one.mkv"),
            post_multipart_file(Session(), "https://two", {}, path, "two.mkv"),
        )
        self.assertEqual(max_active, 1)

    async def test_zero_byte_upload_fails_fast_instead_of_hanging(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path, "wb") as fh:
            fh.write(b"payload")

        class RequestContext:
            async def __aenter__(self):
                await asyncio.Event().wait()

            async def __aexit__(self, *args):
                return False

        class Session:
            def post(self, _url, **kwargs):
                self.kwargs = kwargs
                return RequestContext()

        session = Session()
        with patch.object(http_upload, "_FIRST_BYTE_TIMEOUT", 0.02):
            with self.assertRaisesRegex(RuntimeError, "no upload bytes"):
                await post_multipart_file(
                    session, "https://upload.example", {}, path, "video.mkv",
                )
        self.assertIs(session.kwargs["expect100"], False)

    async def test_freeconvert_processing_task_with_form_is_upload_ready(self):
        task = {
            "name": "import-file",
            "status": "processing",
            "result": {"form": {"url": "https://upload.example", "parameters": {}}},
        }
        with patch.object(
            freeconvert_api, "_fc_get_job",
            new=AsyncMock(return_value={"tasks": [task]}),
        ):
            ready = await freeconvert_api._wait_for_task_ready(
                "key", "job", "import-file", timeout=1,
            )
        self.assertIs(ready, task)

    async def test_cloudconvert_disconnect_confirmation_accepts_finished_import(self):
        job = {"tasks": [{"name": "import-video", "status": "finished"}]}
        with patch.object(
            cloudconvert_api, "check_job_status",
            new=AsyncMock(return_value=job),
        ):
            accepted = await cloudconvert_api._confirm_upload_after_disconnect(
                "key", "job", "import-video", timeout=0.1,
            )
        self.assertTrue(accepted)

    async def test_freeconvert_disconnect_confirmation_accepts_finished_import(self):
        job = {"tasks": [{"name": "import-file", "status": "finished"}]}
        with patch.object(
            freeconvert_api, "_fc_get_job",
            new=AsyncMock(return_value=job),
        ):
            accepted = await freeconvert_api._confirm_upload_after_disconnect(
                "key", "job", "import-file", timeout=0.1,
            )
        self.assertTrue(accepted)

    async def test_payload_has_known_length_and_reports_written_bytes(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path, "wb") as fh:
            fh.write(b"x" * 2_500_000)

        events = []

        async def progress(done, total):
            events.append((done, total))

        class Writer:
            async def write(self, chunk):
                return None

        payload = ProgressFilePayload(
            path, filename="video.mp4", progress_cb=progress,
        )
        await payload.write(Writer())
        form = multipart_upload_form({}, path, "video.mp4")()

        self.assertEqual(payload.size, 2_500_000)
        self.assertGreater(form.size, payload.size)
        self.assertEqual(events[-1], (2_500_000, 2_500_000))

    async def test_stalled_socket_write_fails_instead_of_hanging(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path, "wb") as fh:
            fh.write(b"payload")

        class StalledWriter:
            async def write(self, chunk):
                await asyncio.Event().wait()

        payload = ProgressFilePayload(path, filename="video.mp4")
        payload.write_timeout = 0.01
        with self.assertRaisesRegex(RuntimeError, "Cloud upload stalled"):
            await payload.write(StalledWriter())


if __name__ == "__main__":
    unittest.main()
