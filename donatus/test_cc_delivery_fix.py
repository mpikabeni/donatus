import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


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


class SafePanelUpdaterTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_panel_updater_supports_start_stop_contract(self):
        fake_utils = types.ModuleType("services.utils")
        fake_utils.PanelUpdater = object()
        fake_ccstatus = types.ModuleType("plugins.ccstatus")
        fake_ccstatus._download_export = None

        old_utils = sys.modules.get("services.utils")
        old_ccstatus = sys.modules.get("plugins.ccstatus")
        sys.modules["services.utils"] = fake_utils
        sys.modules["plugins.ccstatus"] = fake_ccstatus

        module_name = "zz_cc_delivery_fix_test"
        try:
            spec = importlib.util.spec_from_file_location(
                module_name,
                Path("plugins/zz_cc_delivery_fix.py"),
            )
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
        finally:
            if old_utils is None:
                sys.modules.pop("services.utils", None)
            else:
                sys.modules["services.utils"] = old_utils
            if old_ccstatus is None:
                sys.modules.pop("plugins.ccstatus", None)
            else:
                sys.modules["plugins.ccstatus"] = old_ccstatus
            sys.modules.pop(module_name, None)

        edits = []

        class FakeMessage:
            async def edit(self, text):
                edits.append(text)

        updater = module.SafePanelUpdater(
            FakeMessage(),
            lambda state: f"done={state.get('done', 0)}",
            interval=0.0,
        )

        started = await updater.start()
        self.assertIs(started, updater)
        updater.tick(done=42)
        await updater.stop()
        self.assertIn("done=42", edits)

        await updater.stop()
        self.assertFalse(updater._stop.is_set() and updater._task is not None)

    async def test_safe_panel_updater_accepts_full_paneupdater_kwargs(self):
        """The patched updater replaces services.utils.PanelUpdater globally,
        so it must accept EVERY kwarg the uploader passes — otherwise every
        delivery upload crashes with 'unexpected keyword argument min_interval'."""
        import inspect

        # The real PanelUpdater's parameter set is the contract to satisfy.
        utils_src = Path("services/utils.py").read_text(encoding="utf-8")
        for kw in ("min_interval", "min_percent_delta", "min_bytes_delta",
                   "max_interval", "start_state"):
            self.assertIn(kw, utils_src)

        fake_utils = types.ModuleType("services.utils")
        fake_utils.PanelUpdater = object()
        fake_ccstatus = types.ModuleType("plugins.ccstatus")
        fake_ccstatus._download_export = None
        old_utils = sys.modules.get("services.utils")
        old_ccstatus = sys.modules.get("plugins.ccstatus")
        sys.modules["services.utils"] = fake_utils
        sys.modules["plugins.ccstatus"] = fake_ccstatus
        try:
            spec = importlib.util.spec_from_file_location(
                "zz_cc_delivery_fix_kwargs", Path("plugins/zz_cc_delivery_fix.py"),
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            for name, old in (("services.utils", old_utils),
                              ("plugins.ccstatus", old_ccstatus)):
                if old is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old
            sys.modules.pop("zz_cc_delivery_fix_kwargs", None)

        # Construct with the exact kwargs the uploader uses — must not raise.
        updater = module.SafePanelUpdater(
            object(),
            lambda state: str(state.get("done", 0)),
            interval=3.0,
            min_interval=3.0,
            min_percent_delta=2.0,
            min_bytes_delta=64 * 1024 * 1024,
            max_interval=15.0,
            start_state={"done": 7},
        )
        # start_state seeds the initial panel state.
        self.assertEqual(updater.state.get("done"), 7)


if __name__ == "__main__":
    unittest.main()
