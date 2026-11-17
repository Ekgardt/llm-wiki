"""Existing retry and opener assertions, isolated from installer executors."""
from __future__ import annotations

import time
import urllib.error
import urllib.request

import pinned_download
import pytest

from tests.slow_machine import LONG_TIMEOUT, SHORT_TIMEOUT

SERVER_URL = "https://example.invalid/x-1.tgz"


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
        resets_twice, deadline=time.monotonic() + LONG_TIMEOUT, sleep=waits.append
    )

    assert (result, len(calls), waits) == ("archive", 3, [1.0, 4.0])


def test_a_permanent_error_is_raised_at_once():
    calls: list[int] = []

    def not_found():
        calls.append(1)
        raise _http_error(404)

    with pytest.raises(urllib.error.HTTPError):
        pinned_download.retry_transient(
            not_found, deadline=time.monotonic() + LONG_TIMEOUT, sleep=lambda _: None
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
            resets, deadline=time.monotonic() + LONG_TIMEOUT, sleep=lambda _: None
        )
    assert len(calls) == 3


class _StubOpener:
    def open(self, request: object, timeout: float | None = None) -> object:
        return request


def test_the_pinned_opener_carries_no_proxy_and_refuses_a_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    def build_opener(*handlers: object) -> _StubOpener:
        captured.extend(handlers)
        return _StubOpener()

    monkeypatch.setattr(pinned_download.urllib.request, "build_opener", build_opener)

    pinned_download.open_pinned_url(
        urllib.request.Request(SERVER_URL), timeout=SHORT_TIMEOUT
    )

    kinds = [type(handler) for handler in captured]
    assert (
        urllib.request.ProxyHandler in kinds,
        pinned_download.RejectRedirect in kinds,
    ) == (True, True)
    assert captured[0].proxies == {}
