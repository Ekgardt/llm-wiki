"""One TCP reset ended a CI install on 2026-09-23; a transient failure is tried again.

See docs/research/2026-09-24-two-post-merge-failures-on-main.md.
"""
from __future__ import annotations

import sys
import time
import urllib.error
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import install_language_server as installer  # noqa: E402
import pinned_download  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid/a", code, "x", {}, None)


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (urllib.error.URLError(ConnectionResetError(104, "reset")), True),
        (ConnectionResetError(104, "reset"), True),
        (TimeoutError("read timed out"), True),
        (_http_error(503), True),
        (_http_error(429), True),
        (_http_error(408), True),
        (_http_error(404), False),
        (_http_error(302), False),
        (pinned_download.PinnedDownloadError("digest"), False),
        (ValueError("x"), False),
    ],
)
def test_the_transient_class_is_the_networks_not_the_pins(error, transient):
    assert pinned_download.is_transient_network_error(error) is transient


def test_a_reset_is_tried_again_and_a_permanent_error_is_not():
    calls: list[int] = []
    waits: list[float] = []

    def resets_twice():
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError(ConnectionResetError(104, "reset"))
        return "archive"

    result = pinned_download.retry_transient(
        resets_twice, deadline=time.monotonic() + SHORT_TIMEOUT, sleep=waits.append
    )

    assert (result, len(calls), waits) == ("archive", 3, [1.0, 4.0])


def test_a_permanent_error_is_raised_at_once():
    calls: list[int] = []

    def not_found():
        calls.append(1)
        raise _http_error(404)

    with pytest.raises(urllib.error.HTTPError):
        pinned_download.retry_transient(
            not_found, deadline=time.monotonic() + SHORT_TIMEOUT, sleep=lambda _: None
        )
    assert len(calls) == 1


def test_the_wait_must_fit_inside_the_deadline():
    calls: list[int] = []

    def resets():
        calls.append(1)
        raise ConnectionResetError(104, "reset")

    with pytest.raises(ConnectionResetError):
        pinned_download.retry_transient(
            resets, deadline=time.monotonic() + 0.5, sleep=lambda _: None
        )
    assert len(calls) == 1


def test_the_third_reset_is_raised():
    calls: list[int] = []

    def resets():
        calls.append(1)
        raise ConnectionResetError(104, "reset")

    with pytest.raises(ConnectionResetError):
        pinned_download.retry_transient(
            resets, deadline=time.monotonic() + SHORT_TIMEOUT, sleep=lambda _: None
        )
    assert len(calls) == 3


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
