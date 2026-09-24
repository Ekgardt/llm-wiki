"""Fetching one pinned artifact: no proxy, no redirect, one absolute deadline.

`scripts/install_pyright.py` worked this way from the start and
`scripts/install_language_server.py` did not: it called `urllib.request.urlopen`,
whose default opener carries a `ProxyHandler` and an `HTTPRedirectHandler`, with
a per-socket timeout and no overall deadline. Rather than keep two copies of the
same loop in step, the opener lives here and both installers use it. Research:
`docs/research/2026-09-17-inst-the-second-installer-gets-the-first-ones-guarantees.md`.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request

from bounded_io import IO_CHUNK_BYTES

NETWORK_TIMEOUT_SECONDS = 30.0
CHUNK_BYTES = IO_CHUNK_BYTES
# The bounds every pinned archive install shares (Pyright and the three other
# managed servers). Pyright's registry metadata reports 5,423 files and
# 19,284,989 unpacked bytes; the others are smaller.
MAX_COMPRESSED_BYTES = 32 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 8192
# One archive member; an exported vault's members are bounded at 16 MiB.
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_PATH_COMPONENTS = 64


class PinnedDownloadError(RuntimeError):
    """The pinned URL did not serve the pinned artifact as pinned."""


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        raise urllib.error.HTTPError(
            request.full_url, code, "redirect refused", headers, file_pointer
        )


def open_pinned_url(
    request: urllib.request.Request,
    *,
    timeout: float,
) -> object:
    """An opener with an empty proxy table and a redirect handler that refuses."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        RejectRedirect(),
        urllib.request.HTTPSHandler(),
    )
    return opener.open(request, timeout=timeout)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("install deadline expired")
    return remaining


def _timeout_setter(response: object) -> object:
    """The open socket's `settimeout`, reached through a private chain.

    `http.client.HTTPResponse` publishes no way to change the socket timeout
    once the connection is open, and an absolute deadline needs every read
    re-clamped. The chain is fail-closed: a response without it is refused
    before any blocking read, so a moved attribute is a red test rather than an
    install that hangs.
    """
    try:
        set_timeout = response.fp.raw._sock.settimeout
    except (AttributeError, TypeError) as exc:
        raise PinnedDownloadError("response cannot be bounded by a deadline") from exc
    if not callable(set_timeout):
        raise PinnedDownloadError("response cannot be bounded by a deadline")
    return set_timeout


def _clamp_read_timeout(response: object, deadline: float) -> None:
    set_timeout = _timeout_setter(response)
    set_timeout(min(NETWORK_TIMEOUT_SECONDS, _remaining(deadline)))


def _content_length(response: object, limit: int) -> int | None:
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    if raw is None:
        return None
    value = _parsed_length(raw)
    if value > limit:
        raise PinnedDownloadError("the response exceeds the compressed bound")
    return value


def _parsed_length(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise PinnedDownloadError("the response declares no usable length") from exc
    if value < 0:
        raise PinnedDownloadError("the response declares no usable length")
    return value


def _require_pinned_response(response: object, url: str) -> None:
    if getattr(response, "status", None) != 200:
        raise PinnedDownloadError(f"{url} did not answer 200")
    if response.geturl() != url:
        raise PinnedDownloadError(f"{url} was served from another address")


def _reader(response: object) -> object:
    read1 = getattr(response, "read1", None)
    if not callable(read1):
        raise PinnedDownloadError("the response cannot be read one chunk at a time")
    return read1


def _next_chunk(response: object, read1: object, deadline: float) -> bytes:
    _clamp_read_timeout(response, deadline)
    chunk = read1(CHUNK_BYTES)
    if not isinstance(chunk, bytes):
        raise PinnedDownloadError("the response yielded something other than bytes")
    return chunk


def _stream(
    response: object, write: object, deadline: float, limit: int, declared: int | None
) -> int:
    read1 = _reader(response)
    total = 0
    while declared is None or total < declared:
        chunk = _next_chunk(response, read1, deadline)
        if not chunk:
            return total
        total += len(chunk)
        _require_within_limit(total, limit)
        write(chunk)
    return total


def _require_within_limit(total: int, limit: int) -> None:
    if total > limit:
        raise PinnedDownloadError("the response exceeds the compressed bound")


def _require_complete(total: int, declared: int | None) -> None:
    if declared is not None and total != declared:
        raise PinnedDownloadError("the response ended before its declared length")


def download_pinned(url: str, write: object, *, deadline: float, limit: int) -> int:
    """Stream one pinned URL into `write`, bounded by `limit` and `deadline`."""
    request = urllib.request.Request(
        url, method="GET", headers={"Accept": "application/octet-stream"}
    )
    with open_pinned_url(request, timeout=_remaining(deadline)) as response:
        _require_pinned_response(response, url)
        declared = _content_length(response, limit)
        total = _stream(response, write, deadline, limit, declared)
    _require_complete(total, declared)
    return total


# Three attempts: the first, then after 1 s and after 4 s. One TCP reset ended a
# CI install on 2026-09-23 (run 35926589114); a reset, a timeout, a 408, a 429 or
# a 5xx is transient, a 4xx, a redirect or a digest mismatch is not.
TRANSIENT_WAITS = (1.0, 4.0)
_TRANSIENT_HTTP = frozenset({408, 429})


def is_transient_network_error(error: BaseException) -> bool:
    """A failure the next attempt may not meet: the network's, not the pin's."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in _TRANSIENT_HTTP or error.code >= 500
    return isinstance(error, (urllib.error.URLError, ConnectionError, TimeoutError))


def _may_retry(error: BaseException, wait: float, deadline: float) -> bool:
    if not is_transient_network_error(error):
        return False
    return time.monotonic() + wait < deadline


def retry_transient(
    operation: object,
    *,
    deadline: float,
    waits: tuple[float, ...] = TRANSIENT_WAITS,
    sleep: object = time.sleep,
) -> object:
    """Run `operation` again after a transient network error, inside the deadline."""
    for wait in waits:
        try:
            return operation()
        except Exception as error:
            if not _may_retry(error, wait, deadline):
                raise
            sleep(wait)
    return operation()
