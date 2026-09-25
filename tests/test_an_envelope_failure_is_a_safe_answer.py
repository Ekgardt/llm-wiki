"""A failure while building the answer's envelope is answered safely, without its path.

See docs/research/2026-09-25-an-envelope-failure-is-a-safe-answer.md.
"""

from __future__ import annotations

import json
import time

import mcp_server


def test_an_envelope_failure_becomes_a_safe_error_envelope(monkeypatch) -> None:
    def broken(*_args, **_kwargs):
        raise OSError("cannot read /home/someone/private/vault/cache/manifest.json")

    monkeypatch.setattr(mcp_server, "_tool_call_envelope", broken)

    text = mcp_server._answer_text("recall", {"query": "q"}, {"results": []}, False, time.monotonic(), time.monotonic() + 5)
    envelope = json.loads(text)

    assert ("error" in envelope["data"], "/home/someone" in text) == (True, False)
