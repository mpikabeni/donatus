import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

# Some older service tests install a minimal aiohttp stub for API-unit tests.
# Add the one attribute fc_webhook imports so this test stays order-independent.
_aiohttp = sys.modules.get("aiohttp")
if _aiohttp is not None and not hasattr(_aiohttp, "web"):
    _aiohttp.web = types.SimpleNamespace()

from plugins import fc_webhook
from services import freeconvert_api
from services.fc_job_store import FCJob


class _UsageResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return {"data": {"minutes_remaining": 17}}


class _UsageSession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, *args, **kwargs):
        return _UsageResponse()


class FreeConvertUsageProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_probe_is_disabled_by_default(self):
        key = "fc-test-key-default-probe"
        freeconvert_api._fc_exhausted.pop(key, None)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FC_USAGE_PROBE", None)
            with patch.object(
                freeconvert_api.aiohttp,
                "ClientSession",
                side_effect=AssertionError("account endpoint must not be called"),
            ):
                score = await freeconvert_api._fc_get_usage(key)

        self.assertEqual(score, 1e6)

    async def test_usage_probe_can_be_enabled_explicitly(self):
        key = "fc-test-key-enabled-probe"
        freeconvert_api._fc_exhausted.pop(key, None)

        with patch.dict(os.environ, {"FC_USAGE_PROBE": "true"}, clear=False):
            with patch.object(
                freeconvert_api.aiohttp,
                "ClientSession",
                _UsageSession,
            ):
                score = await freeconvert_api._fc_get_usage(key)

        self.assertEqual(score, 17.0)


class FreeConvertMissingExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_export_url_releases_claim_for_delayed_retry(self):
        job = FCJob(
            job_id="fc-job-no-export",
            uid=123,
            fname="source.mkv",
            output_name="source.mp4",
            status="completed",
        )
        update = AsyncMock(return_value=job)
        notify = AsyncMock()
        restart_after = Mock()

        with (
            patch.object(fc_webhook, "_extract_download_url", return_value=""),
            patch.object(fc_webhook.fc_job_store, "update", update),
            patch.object(fc_webhook, "_notify_failure", notify),
            patch.object(fc_webhook, "_ensure_fc_poller_after", restart_after),
            patch.object(fc_webhook.time, "time", return_value=1000.0),
        ):
            await fc_webhook._handle_completion(job, {"status": "completed"})

        update.assert_awaited_once_with(
            job.job_id,
            status="processing",
            delivery_not_before=1030.0,
        )
        restart_after.assert_called_once_with(30.0)
        notify.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
