"""Regression tests for video prompt ownership in Pyrofork's dispatcher.

Pyrofork runs at most one matching handler in each group.  These tests inspect
the actual decorator AST rather than relying on fragile source substrings, so a
broad collector cannot silently move in front of video prompt replies again.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _tree(relative_path: str) -> ast.Module:
    path = ROOT / relative_path
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"Function {name!r} was not found")


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _message_handler_group(relative_path: str, function_name: str) -> int:
    node = _function(_tree(relative_path), function_name)
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or _call_name(decorator) != "on_message":
            continue
        for keyword in decorator.keywords:
            if keyword.arg == "group":
                value = ast.literal_eval(keyword.value)
                if not isinstance(value, int):
                    raise AssertionError(f"Non-integer group on {function_name}")
                return value
        return 0
    raise AssertionError(f"No Client.on_message decorator on {function_name}")


class VideoHandlerGroupTests(unittest.TestCase):
    def test_video_text_replies_own_a_dedicated_early_group(self):
        video_group = _message_handler_group("plugins/video.py", "text_reply_handler")
        url_group = _message_handler_group("plugins/url_handler.py", "url_compress_mb_handler")
        resize_group = _message_handler_group("plugins/resize.py", "compress_mb_receiver")

        self.assertEqual(video_group, -1)
        self.assertLess(video_group, url_group)
        self.assertLess(video_group, resize_group)

    def test_mergedone_precedes_the_broad_resize_collector(self):
        mergedone_group = _message_handler_group("plugins/video.py", "cmd_mergedone")
        resize_group = _message_handler_group("plugins/resize.py", "compress_mb_receiver")

        self.assertEqual(mergedone_group, -1)
        self.assertLess(mergedone_group, resize_group)


class VideoTextClaimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Load only the dependency-free claim helpers from plugins.video."""
        tree = _tree("plugins/video.py")
        wanted_assignments = {"_TEXT_REPLY_ACTIONS", "_ACTIVE_TEXT_REPLY_KEYS"}
        wanted_functions = {"_claim_text_reply", "_release_text_reply"}
        selected: list[ast.stmt] = []

        for node in tree.body:
            if isinstance(node, ast.Assign):
                names = {target.id for target in node.targets if isinstance(target, ast.Name)}
                if names & wanted_assignments:
                    selected.append(node)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id in wanted_assignments:
                    selected.append(node)
            elif isinstance(node, ast.FunctionDef) and node.name in wanted_functions:
                selected.append(node)

        namespace = {"FileSession": object}
        module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
        exec(compile(module, "<video-claim-helpers>", "exec"), namespace)
        cls.claim = staticmethod(namespace["_claim_text_reply"])
        cls.release = staticmethod(namespace["_release_text_reply"])
        cls.active = namespace["_ACTIVE_TEXT_REPLY_KEYS"]

    def tearDown(self):
        self.active.clear()

    def test_only_text_prompt_actions_are_claimed(self):
        for action in ("merge_av", "merge_vs", "merge_vids", "burn_sub", None):
            session = SimpleNamespace(key=f"file-{action}", waiting=action)
            self.assertEqual(self.claim(session), (None, False))

        for action in ("trim", "split", "sample", "manual_shots", "rename", "metadata"):
            session = SimpleNamespace(key=f"file-{action}", waiting=action)
            self.assertEqual(self.claim(session), (action, True))
            self.release(session.key)

    def test_duplicate_reply_is_rejected_until_the_claim_is_released(self):
        session = SimpleNamespace(key="same-file", waiting="trim")

        self.assertEqual(self.claim(session), ("trim", True))
        self.assertEqual(self.claim(session), ("trim", False))
        self.release(session.key)
        self.assertEqual(self.claim(session), ("trim", True))

    def test_handler_passes_the_captured_action_to_the_worker(self):
        handler = _function(_tree("plugins/video.py"), "text_reply_handler")
        worker_calls = [
            node for node in ast.walk(handler)
            if isinstance(node, ast.Call) and _call_name(node) == "_text_reply_work"
        ]
        self.assertEqual(len(worker_calls), 1)
        action_keywords = [kw.value for kw in worker_calls[0].keywords if kw.arg == "action"]
        self.assertEqual(len(action_keywords), 1)
        self.assertIsInstance(action_keywords[0], ast.Name)
        self.assertEqual(action_keywords[0].id, "action")


class VideoPromptPersistenceTests(unittest.TestCase):
    def test_prompt_state_is_saved_through_the_waiting_helper(self):
        tree = _tree("plugins/video.py")
        helper = _function(tree, "_set_waiting")
        prompt = _function(tree, "_prompt")

        touch_calls = [
            node for node in ast.walk(helper)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sessions"
            and node.func.attr == "touch"
        ]
        self.assertEqual(len(touch_calls), 1)

        prompt_calls = [
            node for node in ast.walk(prompt)
            if isinstance(node, ast.Call) and _call_name(node) == "_set_waiting"
        ]
        literal_actions = {
            ast.literal_eval(call.args[1])
            for call in prompt_calls
            if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant)
        }
        self.assertTrue(
            {"merge_av", "merge_vs", "burn_sub", "merge_vids", "rename", "metadata"}
            <= literal_actions
        )
        self.assertTrue(
            any(
                len(call.args) >= 2
                and isinstance(call.args[1], ast.Name)
                and call.args[1].id == "action"
                for call in prompt_calls
            )
        )

    def test_active_text_task_blocks_callbacks_and_is_cancellable(self):
        tree = _tree("plugins/video.py")
        guard = _function(tree, "_reject_active_callback")
        tracked = _function(tree, "_tracked_ffmpeg")

        self.assertTrue(
            any(
                isinstance(node, ast.Name)
                and node.id == "_ACTIVE_TEXT_REPLY_KEYS"
                for node in ast.walk(guard)
            )
        )
        for callback_name in (
            "video_cb",
            "smap_select_cb",
            "stream_cb",
            "audio_fmt_cb",
            "video_convert_platform_cb",
            "video_conv_cb",
        ):
            callback = _function(tree, callback_name)
            self.assertTrue(
                any(
                    isinstance(node, ast.Call)
                    and _call_name(node) == "_reject_active_callback"
                    for node in ast.walk(callback)
                ),
                callback_name,
            )
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register_raw"
                for node in ast.walk(tracked)
            )
        )

    def test_split_worker_uses_exact_validated_helper_results(self):
        worker = _function(_tree("plugins/video.py"), "_text_reply_work")
        source = ast.unparse(worker)
        self.assertIn("parts_done = await _tracked_ffmpeg", source)
        self.assertNotIn("os.listdir", source)


if __name__ == "__main__":
    unittest.main()
