"""
Behavioural tests for the runtime-patch signature guard.

These are deliberately NOT source-assertion string pins. Every test here calls
the code and asserts on what it does, because the outage this guard exists to
prevent (SafePanelUpdater drifting out of sync with PanelUpdater and killing
every upload with "unexpected keyword argument 'min_interval'") was invisible
to string pins — the literals all still matched.
"""
import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path

from services import patch_guard


def _load_safe_panel_updater():
    """Load SafePanelUpdater from plugins/zz_cc_delivery_fix.py in isolation.

    Importing the plugin normally pulls in plugins.ccstatus -> core.config,
    which sys.exit(1)s without real bot credentials. Injecting stand-ins for
    the two modules it patches lets the class under test load on its own, the
    same approach tests/test_cc_delivery_fix.py already uses.
    """
    import services
    import plugins

    fake_utils = types.ModuleType("services.utils")
    fake_utils.PanelUpdater = object()
    fake_ccstatus = types.ModuleType("plugins.ccstatus")
    fake_ccstatus._download_export = None

    fakes = {"services.utils": fake_utils, "plugins.ccstatus": fake_ccstatus}
    parents = {"services.utils": (services, "utils"),
               "plugins.ccstatus": (plugins, "ccstatus")}

    saved_modules = {name: sys.modules.get(name) for name in fakes}
    # `import services.utils as utils` binds through the PARENT package
    # attribute, so shadowing sys.modules alone is not enough — without this
    # the probe patches the real services.utils and corrupts every later test
    # in the same process.
    _MISSING = object()
    saved_attrs = {name: getattr(pkg, attr, _MISSING)
                   for name, (pkg, attr) in parents.items()}

    for name, fake in fakes.items():
        sys.modules[name] = fake
        pkg, attr = parents[name]
        setattr(pkg, attr, fake)
    try:
        path = Path(__file__).resolve().parent.parent / "plugins" / "zz_cc_delivery_fix.py"
        spec = importlib.util.spec_from_file_location("_zz_cc_delivery_probe", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.SafePanelUpdater
    finally:
        for name in fakes:
            mod = saved_modules[name]
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
            pkg, attr = parents[name]
            old = saved_attrs[name]
            if old is _MISSING:
                if hasattr(pkg, attr):
                    delattr(pkg, attr)
            else:
                setattr(pkg, attr, old)


# ── Fixtures modelling the real outage ───────────────────────────────────────

class _Original:
    """Stands in for services.utils.PanelUpdater."""

    def __init__(self, msg, build_fn, interval=3.0, min_interval=3.0,
                 min_percent_delta=2.0, min_bytes_delta=64 * 1024 * 1024,
                 max_interval=15.0, important_final_update=False,
                 start_state=None):
        self.msg = msg
        self.build_fn = build_fn


class _BrokenReplacement:
    """The pre-outage SafePanelUpdater: dropped almost every parameter."""

    def __init__(self, msg, build_fn, interval=2.0):
        pass


class _RenamedReplacement:
    """Accepts everything by count, but renamed build_fn -> builder.

    A **kwargs catch-all makes this look fine to a naive check while silently
    swallowing the callback: exactly the class of bug that shipped.
    """

    def __init__(self, msg, builder, interval=2.0, min_interval=3.0,
                 min_percent_delta=2.0, min_bytes_delta=64 * 1024 * 1024,
                 max_interval=15.0, important_final_update=False,
                 start_state=None, **_ignored):
        pass


class _GoodReplacement:
    """Name-for-name compatible, plus a forward-compatible catch-all."""

    def __init__(self, msg, build_fn, interval=2.0, min_interval=3.0,
                 min_percent_delta=2.0, min_bytes_delta=64 * 1024 * 1024,
                 max_interval=15.0, important_final_update=False,
                 start_state=None, **_ignored):
        pass


def _original_fn(job, data, retries=3):
    return None


def _forwarding_wrapper(job, *args, **kwargs):
    return _original_fn(job, *args, **kwargs)


def _narrow_wrapper(job):
    return None


def _demanding_replacement(job, data, retries=3, *, required_new):
    return None


class CheckCompatibleTests(unittest.TestCase):
    def test_pre_outage_replacement_is_rejected(self):
        """The exact drift that broke every upload must be caught."""
        ok, reason = patch_guard.check_compatible(
            _BrokenReplacement, _Original, wrapper=False)
        self.assertFalse(ok)
        # It should name the parameter whose absence produced the TypeError.
        self.assertIn("min_interval", reason)

    def test_renamed_parameter_is_rejected_for_reimplementation(self):
        """**kwargs must not excuse a rename in a reimplementation: the value
        would land in the ignored dict instead of being used."""
        ok, reason = patch_guard.check_compatible(
            _RenamedReplacement, _Original, wrapper=False)
        self.assertFalse(ok)
        self.assertIn("build_fn", reason)

    def test_compatible_replacement_is_accepted(self):
        ok, reason = patch_guard.check_compatible(
            _GoodReplacement, _Original, wrapper=False)
        self.assertTrue(ok, reason)

    def test_forwarding_wrapper_is_accepted(self):
        ok, reason = patch_guard.check_compatible(
            _forwarding_wrapper, _original_fn, wrapper=True)
        self.assertTrue(ok, reason)

    def test_wrapper_that_drops_positionals_is_rejected(self):
        ok, reason = patch_guard.check_compatible(
            _narrow_wrapper, _original_fn, wrapper=True)
        self.assertFalse(ok)

    def test_replacement_requiring_new_args_is_rejected(self):
        """A replacement cannot demand something the original never passes."""
        ok, reason = patch_guard.check_compatible(
            _demanding_replacement, _original_fn, wrapper=False)
        self.assertFalse(ok)
        self.assertIn("required_new", reason)

    def test_positional_rename_is_caught_even_for_wrappers(self):
        def renamed_positional(task, data, retries=3):
            return None
        ok, reason = patch_guard.check_compatible(
            renamed_positional, _original_fn, wrapper=True)
        self.assertFalse(ok)
        self.assertIn("job", reason)


class SafePatchTests(unittest.TestCase):
    def setUp(self):
        patch_guard._REGISTRY.clear()

    def tearDown(self):
        patch_guard._REGISTRY.clear()

    def _module(self):
        import types as _t
        mod = _t.ModuleType("fake_target_module")
        mod.PanelUpdater = _Original
        return mod

    def test_incompatible_patch_is_refused_and_original_survives(self):
        """The critical property: a bad patch must NOT be installed, so the
        bot keeps running on stock behaviour instead of failing every call."""
        mod = self._module()
        installed = patch_guard.safe_patch(
            mod, "PanelUpdater", _BrokenReplacement,
            label="test/panel", wrapper=False)

        self.assertFalse(installed)
        self.assertIs(mod.PanelUpdater, _Original, "original was replaced anyway")

        rec = patch_guard._REGISTRY["test/panel"]
        self.assertEqual(rec.status, patch_guard.REFUSED)
        self.assertFalse(rec.healthy)

    def test_compatible_patch_is_installed(self):
        mod = self._module()
        installed = patch_guard.safe_patch(
            mod, "PanelUpdater", _GoodReplacement,
            label="test/panel", wrapper=False)

        self.assertTrue(installed)
        self.assertIs(mod.PanelUpdater, _GoodReplacement)
        self.assertEqual(patch_guard._REGISTRY["test/panel"].status,
                         patch_guard.ACTIVE)

    def test_missing_target_is_recorded_not_raised(self):
        import types as _t
        mod = _t.ModuleType("empty_module")
        installed = patch_guard.safe_patch(
            mod, "does_not_exist", _GoodReplacement, label="test/missing")
        self.assertFalse(installed)
        self.assertEqual(patch_guard._REGISTRY["test/missing"].status,
                         patch_guard.FAILED)

    def test_unhealthy_and_report_lines_surface_refusals(self):
        mod = self._module()
        patch_guard.safe_patch(mod, "PanelUpdater", _BrokenReplacement,
                               label="test/bad", wrapper=False)
        patch_guard.record("test/good", patch_guard.ACTIVE)

        bad = patch_guard.unhealthy()
        self.assertEqual([r.label for r in bad], ["test/bad"])

        lines = "\n".join(patch_guard.report_lines())
        self.assertIn("test/bad", lines)
        self.assertIn("refused", lines)


class SafePanelUpdaterContractTests(unittest.TestCase):
    """The regression test for the actual outage.

    Constructs the live SafePanelUpdater exactly the way real callers construct
    PanelUpdater. Had this existed, the 'unexpected keyword argument
    min_interval' outage would have failed in CI instead of in production.
    """

    def setUp(self):
        from services import utils
        self.Safe = _load_safe_panel_updater()
        self.Real = utils.PanelUpdater

    def test_safe_updater_accepts_every_real_parameter_by_keyword(self):
        params = inspect.signature(self.Real.__init__).parameters
        kwargs = {
            name: p.default
            for name, p in params.items()
            if name not in ("self", "msg", "build_fn")
            and p.default is not inspect.Parameter.empty
        }
        self.assertIn("min_interval", kwargs, "fixture drifted from PanelUpdater")

        # Must not raise TypeError — this is the exact failure that shipped.
        updater = self.Safe(object(), lambda state: "panel", **kwargs)
        self.assertIsNotNone(updater)

    def test_safe_updater_accepts_build_fn_by_keyword(self):
        """Callers may pass the builder by name; a rename must not swallow it."""
        sentinel = lambda state: "rendered"
        updater = self.Safe(msg=object(), build_fn=sentinel)
        self.assertIs(updater.build_fn, sentinel)

    def test_safe_updater_renders_through_the_supplied_builder(self):
        calls = []

        def builder(state):
            calls.append(state)
            return f"done={state.get('done', 0)}"

        updater = self.Safe(object(), builder, start_state={"done": 7})
        self.assertEqual(updater.build_fn(dict(updater.state)), "done=7")
        self.assertEqual(calls[-1]["done"], 7)

    def test_signature_guard_accepts_the_live_pair(self):
        """The shipped SafePanelUpdater must satisfy the guard, otherwise the
        patch silently stops installing and CC delivery regresses."""
        ok, reason = patch_guard.check_compatible(
            self.Safe, self.Real, wrapper=False)
        # PanelUpdater may already be patched; compare against whichever class
        # is genuinely the stock one.
        if self.Safe is self.Real:
            self.skipTest("PanelUpdater already replaced by SafePanelUpdater")
        self.assertTrue(ok, f"SafePanelUpdater drifted from PanelUpdater: {reason}")


class BoundedHTTPSessionTests(unittest.TestCase):
    """Every aiohttp session must carry a timeout.

    An unbounded ClientSession is the exact bug class behind the hangs this
    repo kept hitting: the await never returns, and the caller is usually
    holding a per-message panel lock.
    """

    def test_no_unbounded_client_session_in_repo(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        pattern = re.compile(r"\baiohttp\.ClientSession\(\s*\)")
        offenders = []
        for path in list(root.glob("services/*.py")) + list(root.glob("plugins/*.py")):
            text = path.read_text(encoding="utf-8")
            for num, line in enumerate(text.splitlines(), 1):
                if pattern.search(line.replace("_aiohttp.", "aiohttp.")):
                    offenders.append(f"{path.relative_to(root)}:{num}")

        self.assertEqual(
            offenders, [],
            "ClientSession() without a timeout can hang forever; pass "
            "timeout=aiohttp.ClientTimeout(...):\n  " + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
