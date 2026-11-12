"""Helpers shared by test_contract.py and the test files importing them."""
from __future__ import annotations

import hashlib
from pathlib import Path

from lsp_server_profile import PackageLaunch

_SERVER_BYTES = b"console.log('server');\n"


_MANIFEST = {"name": "example-language-server", "version": "1.2.3", "type": "module"}


def _launch() -> PackageLaunch:
    return PackageLaunch(entry_relative=Path("lib/cli.mjs"), manifest=_MANIFEST)


def _digest(payload: bytes = _SERVER_BYTES) -> str:
    return hashlib.sha256(payload).hexdigest()
