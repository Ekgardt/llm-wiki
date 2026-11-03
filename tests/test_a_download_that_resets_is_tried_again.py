"""One TCP reset ended a CI install on 2026-09-23; a transient failure is tried again.

See docs/research/2026-09-24-two-post-merge-failures-on-main.md.
"""
from __future__ import annotations

import sys
import time
import urllib.error
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import install_language_server as installer  # noqa: E402
import pinned_download  # noqa: E402


def test_the_installer_truncates_a_partial_file_between_attempts(tmp_path, monkeypatch):
    """A reset mid-stream leaves bytes behind; the next attempt starts empty."""
    attempts: list[int] = []

    def download(url, write, *, deadline, limit):
        attempts.append(1)
        write(b"partial-")
        if len(attempts) == 1:
            raise urllib.error.URLError(ConnectionResetError(104, "reset"))
        write(b"whole")
        return 13

    monkeypatch.setattr(installer, "download_pinned", download)
    monkeypatch.setattr(pinned_download.time, "sleep", lambda _: None)
    target = tmp_path / "archive"

    installer._downloaded("https://example.invalid/a", target, time.monotonic() + 60, 1 << 20)

    assert (len(attempts), target.read_bytes()) == (2, b"partial-whole")
