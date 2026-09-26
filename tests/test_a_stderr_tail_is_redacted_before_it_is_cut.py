"""A failed server's stderr tail keeps no credential a cut or a line split hid (audit C-37).

docs/research/2026-09-25-a-stderr-tail-is-redacted-before-it-is-cut.md
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from lsp_process import _failure_stderr_tail  # noqa: E402
from lsp_security import redact_private_key_blocks  # noqa: E402

BODY = b"MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n"
BEGIN = b"-----BEGIN PRIVATE KEY-----\n"
END = b"-----END PRIVATE KEY-----\n"


def _tail(*chunks: bytes) -> str:
    generation = SimpleNamespace(stderr=list(chunks), stderr_lock=threading.Lock())
    return _failure_stderr_tail(generation) or ""


def test_a_value_whose_key_fell_before_the_cut_is_still_redacted():
    tail = _tail(b"password=" + b"Q" * 2000 + b"\n", b"server exited\n")
    assert ("Q" * 16 not in tail, tail.endswith("server exited")) == (True, True)


def test_a_private_key_block_is_redacted_whole():
    tail = _tail(b"loading key\n", BEGIN, BODY * 3, END, b"server exited\n")
    assert (BODY.decode().strip() in tail, "<redacted private key>" in tail) == (False, True)


def test_a_key_whose_begin_fell_outside_the_window_is_redacted():
    tail = _tail(BEGIN, BODY * 1400, END, b"server exited\n")
    assert (BODY.decode().strip() in tail, tail.endswith("server exited")) == (False, True)


def test_an_unterminated_key_is_redacted_to_the_end():
    text = (BEGIN + BODY * 2).decode()
    assert redact_private_key_blocks("before\n" + text) == "before\n<redacted private key>"
