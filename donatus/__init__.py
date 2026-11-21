"""Test package.

Installs the pyrogram stub before any test module imports a plugin. This runs
for both `python -m unittest discover -s tests` (CI) and
`python -m unittest tests.test_x` (local).

The stub is a no-op when the real pyrogram/pyrofork is installed, so CI keeps
testing against the actual library.
"""
from tests import _pyrogram_stub as _stub

_stub.install()
