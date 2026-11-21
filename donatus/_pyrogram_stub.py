"""
tests/_pyrogram_stub.py
Minimal pyrogram stand-in so the suite runs without pyrofork installed.

WHY
───
Roughly half the test modules import a plugin, and every plugin imports
pyrogram. On a dev machine without pyrofork those modules failed to import at
all, so CI was the only place the suite ever ran fully — and CI was off for
six merges. Everything that broke in that window (the SafePanelUpdater outage
included) had no local gate to catch it.

This stub is only installed when the real pyrogram is NOT importable. CI
installs pyrofork from requirements.txt and therefore keeps testing against
the real library — the stub never masks a genuine pyrogram incompatibility
there. Locally it trades that fidelity for the ability to run at all.

It emulates pyrofork 2.3.69's surface where the code under test inspects it,
notably Client.save_file's parameter list, so plugins/zz_fast_upload.py's
version guard exercises its real accept path instead of its bail-out path.
"""
from __future__ import annotations

import sys
import types

__all__ = ["install", "STUBBED"]

STUBBED = False


def _is_real_pyrogram_available() -> bool:
    if "pyrogram" in sys.modules:
        return not getattr(sys.modules["pyrogram"], "_ZILONG_TEST_STUB", False)
    try:
        import pyrogram  # noqa: F401
    except Exception:
        return False
    return True


class _Filter:
    """Composable no-op filter supporting &, |, ~ like pyrogram's."""

    def __init__(self, name: str = "filter"):
        self.name = name

    def __and__(self, other): return _Filter(f"({self.name} & {other})")
    def __or__(self, other): return _Filter(f"({self.name} | {other})")
    def __invert__(self): return _Filter(f"~{self.name}")
    def __rand__(self, other): return _Filter(f"({other} & {self.name})")
    def __ror__(self, other): return _Filter(f"({other} | {self.name})")
    def __call__(self, *a, **k): return True
    def __repr__(self): return f"<Filter {self.name}>"


class _Filters:
    private = _Filter("private")
    group = _Filter("group")
    channel = _Filter("channel")
    text = _Filter("text")
    photo = _Filter("photo")
    video = _Filter("video")
    document = _Filter("document")
    audio = _Filter("audio")
    incoming = _Filter("incoming")
    me = _Filter("me")
    all = _Filter("all")

    @staticmethod
    def command(cmds, *a, **k): return _Filter(f"command({cmds})")

    @staticmethod
    def user(uids, *a, **k): return _Filter(f"user({uids})")

    @staticmethod
    def chat(cids, *a, **k): return _Filter(f"chat({cids})")

    @staticmethod
    def regex(pattern, *a, **k): return _Filter(f"regex({pattern})")

    @staticmethod
    def create(func, name=None, **k): return _Filter(name or "custom")


def _decorator(*d_args, **d_kwargs):
    """Handler decorator: registers nothing, returns the function unchanged."""
    def wrap(func):
        return func
    return wrap


class _Client:
    """Stand-in for pyrogram.Client.

    Handler decorators are classmethod-style no-ops so module-level
    `@Client.on_message(...)` works at import time.
    """

    on_message = staticmethod(_decorator)
    on_callback_query = staticmethod(_decorator)
    on_edited_message = staticmethod(_decorator)
    on_inline_query = staticmethod(_decorator)
    on_chat_member_updated = staticmethod(_decorator)
    on_raw_update = staticmethod(_decorator)
    on_disconnect = staticmethod(_decorator)
    on_deleted_messages = staticmethod(_decorator)

    def __init__(self, *a, **k):
        self.me = None

    # Pinned to pyrofork 2.3.69's layout. plugins/zz_fast_upload.py compares
    # this parameter list exactly before installing its upload boost.
    async def save_file(self, path, file_id: int = None, file_part: int = 0,
                        progress=None, progress_args: tuple = ()):
        return None

    async def send_message(self, *a, **k): return _Message()
    async def send_video(self, *a, **k): return _Message()
    async def send_document(self, *a, **k): return _Message()
    async def send_photo(self, *a, **k): return _Message()
    async def edit_message_text(self, *a, **k): return _Message()
    async def get_messages(self, *a, **k): return _Message()
    async def start(self): return self
    async def stop(self): return self


class _Obj:
    """Permissive attribute bag for pyrogram model types."""

    def __init__(self, *a, **k):
        for key, val in k.items():
            setattr(self, key, val)

    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        return None


class _Message(_Obj):
    async def reply(self, *a, **k): return _Message()
    async def reply_text(self, *a, **k): return _Message()
    async def edit(self, *a, **k): return _Message()
    async def edit_text(self, *a, **k): return _Message()
    async def delete(self, *a, **k): return True


class _CallbackQuery(_Obj):
    async def answer(self, *a, **k): return True


class _InlineKeyboardButton:
    def __init__(self, text, callback_data=None, url=None, **k):
        self.text = text
        self.callback_data = callback_data
        self.url = url

    def __repr__(self):
        return f"<Button {self.text!r} cb={self.callback_data!r}>"


class _InlineKeyboardMarkup:
    def __init__(self, inline_keyboard=None, **k):
        # Real rows, so tests that walk the keyboard to check labels and
        # mobile-safe row widths operate on genuine structure.
        self.inline_keyboard = list(inline_keyboard or [])

    def __iter__(self):
        return iter(self.inline_keyboard)

    def __getitem__(self, idx):
        return self.inline_keyboard[idx]

    def __len__(self):
        return len(self.inline_keyboard)


class _ParseMode:
    HTML = "html"
    MARKDOWN = "markdown"
    DEFAULT = "default"
    DISABLED = "disabled"


class _ChatType:
    PRIVATE = "private"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    CHANNEL = "channel"


class _MessageMediaType:
    VIDEO = "video"
    DOCUMENT = "document"
    PHOTO = "photo"
    AUDIO = "audio"


class _ChatAction:
    TYPING = "typing"
    UPLOAD_VIDEO = "upload_video"
    UPLOAD_DOCUMENT = "upload_document"


_ERROR_NAMES = [
    "RPCError", "FloodWait", "MessageNotModified", "MessageIdInvalid",
    "MessageDeleteForbidden", "ChannelInvalid", "ChatWriteForbidden",
    "PeerIdInvalid", "UserIsBlocked", "BadRequest", "Forbidden",
    "Unauthorized", "InternalServerError", "MessageEmpty", "MediaEmpty",
    "PhotoInvalidDimensions", "WebpageCurlFailed", "AuthKeyUnregistered",
    "SessionRevoked", "UserDeactivated", "FileReferenceExpired",
]


def _module(name: str, **attrs) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None or not isinstance(mod, types.ModuleType):
        mod = types.ModuleType(name)
    for key, val in attrs.items():
        setattr(mod, key, val)
    sys.modules[name] = mod
    return mod


def install() -> bool:
    """Install the stub if real pyrogram is unavailable. Returns True if stubbed."""
    global STUBBED
    if STUBBED:
        return True
    if _is_real_pyrogram_available():
        return False

    errors = {name: type(name, (Exception,), {
        "__init__": lambda self, *a, **k: Exception.__init__(self, *a)
    }) for name in _ERROR_NAMES}
    # FloodWait carries a .value callers sleep on.
    class FloodWait(Exception):
        def __init__(self, *a, **k):
            Exception.__init__(self, *a)
            self.value = k.get("value", 1)
            self.x = self.value
    errors["FloodWait"] = FloodWait

    enums_mod = _module(
        "pyrogram.enums",
        ParseMode=_ParseMode, ChatType=_ChatType,
        MessageMediaType=_MessageMediaType, ChatAction=_ChatAction,
    )
    types_mod = _module(
        "pyrogram.types",
        Message=_Message, CallbackQuery=_CallbackQuery,
        InlineKeyboardButton=_InlineKeyboardButton,
        InlineKeyboardMarkup=_InlineKeyboardMarkup,
        User=_Obj, Chat=_Obj, Video=_Obj, Document=_Obj, Photo=_Obj,
        Audio=_Obj, Thumbnail=_Obj, InputMediaPhoto=_Obj,
        InputMediaVideo=_Obj, InputMediaDocument=_Obj,
        ReplyKeyboardMarkup=_Obj, KeyboardButton=_Obj,
        ForceReply=_Obj, BotCommand=_Obj,
    )
    errors_mod = _module("pyrogram.errors", **errors)
    _module("pyrogram.errors.exceptions", **errors)
    _module("pyrogram.client", Client=_Client)
    _module("pyrogram.file_id", FileId=_Obj, FileType=_Obj)
    _module("pyrogram.session", Session=_Obj, Auth=_Obj)
    _module("pyrogram.raw", functions=_Obj(), types=_Obj())
    _module("pyrogram.utils", **{})

    filters_mod = _module("pyrogram.filters")
    for attr in dir(_Filters):
        if not attr.startswith("_"):
            setattr(filters_mod, attr, getattr(_Filters, attr))

    root = _module(
        "pyrogram",
        Client=_Client,
        filters=filters_mod,
        enums=enums_mod,
        types=types_mod,
        errors=errors_mod,
        StopTransmission=type("StopTransmission", (Exception,), {}),
        __version__="0.0.0-zilong-test-stub",
        _ZILONG_TEST_STUB=True,
    )
    root.client = sys.modules["pyrogram.client"]
    root.session = sys.modules["pyrogram.session"]
    root.raw = sys.modules["pyrogram.raw"]

    STUBBED = True
    return True
