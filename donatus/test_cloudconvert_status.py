import sys
import types
import unittest


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class ClientSession:
        pass

    aiohttp_stub.ClientTimeout = ClientTimeout
    aiohttp_stub.ClientSession = ClientSession
    sys.modules["aiohttp"] = aiohttp_stub

from services import cloudconvert_api


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class CloudConvertJobStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old_session = cloudconvert_api.aiohttp.ClientSession

    def tearDown(self):
        cloudconvert_api.aiohttp.ClientSession = self._old_session

    async def test_invalid_first_key_does_not_hide_job_owned_by_second_key(self):
        calls = []
        responses = {
            "bad-key": _FakeResponse(401, {"message": "Unauthenticated"}),
            "good-key": _FakeResponse(
                200,
                {"data": {"id": "job-123", "status": "finished"}},
            ),
        }

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, url, headers):
                key = headers["Authorization"].removeprefix("Bearer ")
                calls.append((url, key))
                return responses[key]

        cloudconvert_api.aiohttp.ClientSession = FakeSession

        job = await cloudconvert_api.check_job_status("bad-key, good-key", "job-123")

        self.assertEqual(job, {"id": "job-123", "status": "finished"})
        self.assertEqual([key for _, key in calls], ["bad-key", "good-key"])

    async def test_retryable_failures_continue_to_next_key(self):
        calls = []
        responses = iter(
            [
                _FakeResponse(429, {"message": "Rate limit exceeded"}),
                _FakeResponse(503, {"message": "Temporarily unavailable"}),
                _FakeResponse(200, {"data": {"id": "job-456", "status": "processing"}}),
            ]
        )

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, url, headers):
                calls.append(headers["Authorization"])
                return next(responses)

        cloudconvert_api.aiohttp.ClientSession = FakeSession

        job = await cloudconvert_api.check_job_status("key-a,key-b,key-c", "job-456")

        self.assertEqual(job["status"], "processing")
        self.assertEqual(calls, ["Bearer key-a", "Bearer key-b", "Bearer key-c"])

    async def test_all_failed_keys_raise_last_actionable_http_error(self):
        responses = iter(
            [
                _FakeResponse(403, {"message": "Forbidden"}),
                _FakeResponse(404, {"message": "Job not found"}),
                _FakeResponse(503, {"message": "Provider maintenance"}),
            ]
        )

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, url, headers):
                return next(responses)

        cloudconvert_api.aiohttp.ClientSession = FakeSession

        with self.assertRaisesRegex(
            RuntimeError,
            r"job-789.*HTTP 503: Provider maintenance",
        ):
            await cloudconvert_api.check_job_status("key-a,key-b,key-c", "job-789")


if __name__ == "__main__":
    unittest.main()
