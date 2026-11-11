"""Server progress, work-done tokens, the progress gate and `workspace/configuration` answers."""

import time
from collections.abc import Mapping
from types import MappingProxyType

from llm_wiki.pyright_session_ranges import bounded_text

# workspace/configuration: 64 items of 256-byte section names refuse a malformed request. Values
# predate measurement; review if a pinned server is refused its configuration.
_MAX_CONFIGURATION_ITEMS = 64
_MAX_CONFIGURATION_SECTION_BYTES = 256
# Server progress kept as a ring: the last 256 events or 1 MiB, each costed 32 bytes plus text;
# the oldest event leaves first. Values predate measurement.
_MAX_PROGRESS_TEXT_BYTES = 4096
_MAX_PROGRESS_EVENTS = 256
_MAX_PROGRESS_BYTES = 1024 * 1024
# The fixed cost charged per progress event, on top of its text (see the ring above).
_PROGRESS_EVENT_BASE_BYTES = 32


# `$/progress` is the specification's; the rest a profile declares for itself.
_NEUTRAL_PROGRESS_METHOD = "$/progress"


# How long a query waits between checks for the work-done-progress `end` that a
# progress-gated profile makes readiness out of. Short because the whole gate is
# measured at 0.67-0.82 s on a small project and the caller holds a deadline.
_PROGRESS_POLL_SECONDS = 0.02


# How long a query waits for a progress-gated server that has not asked us to
# create any work-done token. A server that reports progress asks during
# initialization, before it answers anything, so this only has to outlast the
# gap between the handshake and the first query -- not a project load.
PROGRESS_GATE_GRACE_SECONDS = 1.0


# At most this many outstanding work-done tokens are remembered per generation.
# A server that opens more than this without ending them is not one we can gate
# on, and the bound keeps a misbehaving one from growing the session.
_MAX_WORK_DONE_TOKENS = 64


def _progress_token(value: object) -> str | int | None:
    """The progress token, bounded when textual; None when it is unusable."""
    if isinstance(value, str):
        return bounded_text(value, 256)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _progress_text(value: Mapping[str, object], kind: str) -> tuple[str | None, bool]:
    """The progress text, and whether the field was readable at all."""
    raw = value.get("title") if kind == "begin" else value.get("message")
    if raw is None:
        return "", True
    text = bounded_text(raw, _MAX_PROGRESS_TEXT_BYTES)
    return text, text is not None


def _progress_payload(params: object) -> tuple[object, Mapping[str, object]] | None:
    """The token and value of a `$/progress` payload, when it is well formed."""
    if not isinstance(params, dict) or set(params) != {"token", "value"}:
        return None
    value = params["value"]
    if not isinstance(value, dict):
        return None
    return params["token"], value


def _progress_record(method: str, params: object) -> tuple[object, ...] | None:
    """The `$/progress` record to retain, or None when the payload is unusable."""
    payload = _progress_payload(params)
    if payload is None:
        return None
    raw_token, value = payload
    token = _progress_token(raw_token)
    kind = value.get("kind")
    if token is None or kind not in {"begin", "report", "end"}:
        return None
    return _progress_text_record(method, token, kind, value)


def _progress_text_record(
    method: str, token: object, kind: object, value: Mapping[str, object]
) -> tuple[object, ...] | None:
    text, readable = _progress_text(value, kind)
    if not readable:
        return None
    return method, token, kind, text


def _work_done_end_token(params: object) -> object | None:
    """The token a `$/progress` `end` closes, when the payload is well formed."""
    payload = _progress_payload(params)
    if payload is None:
        return None
    raw_token, value = payload
    if value.get("kind") != "end":
        return None
    return _progress_token(raw_token)


def _pyright_marker_record(method: str, params: object) -> tuple[object, ...] | None:
    """A begin/end marker carries no payload of its own."""
    if params is None or params == {}:
        return (method,)
    return None


def _pyright_report_record(method: str, params: object) -> tuple[object, ...] | None:
    """A progress report carries one bounded message."""
    value = params.get("message") if isinstance(params, dict) else params
    text = bounded_text(value, _MAX_PROGRESS_TEXT_BYTES)
    if text is None:
        return None
    return method, text


_PROGRESS_RECORDS = MappingProxyType(
    {
        "$/progress": _progress_record,
        "pyright/beginProgress": _pyright_marker_record,
        "pyright/endProgress": _pyright_marker_record,
        "pyright/reportProgress": _pyright_report_record,
    }
)


def _progress_notification_record(
    method: str, params: object
) -> tuple[object, ...] | None:
    """What to retain for this progress notification, if anything."""
    build = _PROGRESS_RECORDS.get(method)
    if build is None:
        return None
    return build(method, params)


def _configuration_items(params: object) -> list[object] | None:
    """The configuration items requested, when the request is well formed."""
    if not isinstance(params, dict) or set(params) - {"items"}:
        return None
    items = params.get("items")
    if not isinstance(items, list) or len(items) > _MAX_CONFIGURATION_ITEMS:
        return None
    return items


def _usable_scope_uri(scope_uri: object) -> bool:
    """An absent scope is fine; a present one has to be bounded text."""
    if scope_uri is None:
        return True
    if not isinstance(scope_uri, str):
        return False
    return len(scope_uri.encode("utf-8", errors="strict")) <= 16 * 1024


def _usable_section(section: object) -> bool:
    """A section name has to be non-empty, encodable and bounded."""
    if not isinstance(section, str) or not section:
        return False
    try:
        size = len(section.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return False
    return size <= _MAX_CONFIGURATION_SECTION_BYTES


def _configuration_item_ok(item: object) -> bool:
    """The item names only fields we accept, and carries a usable scope."""
    if not isinstance(item, dict) or set(item) - {"scopeUri", "section"}:
        return False
    return _usable_scope_uri(item.get("scopeUri"))


def _configuration_section(item: object) -> tuple[str | None, bool]:
    """The section requested, and whether the item was usable at all."""
    if not _configuration_item_ok(item):
        return None, False
    assert isinstance(item, dict)
    return _usable_configuration_section(item.get("section"))


def _usable_configuration_section(section: object) -> tuple[str | None, bool]:
    """An absent section is usable; a present one has to be bounded text."""
    if section is None:
        return None, True
    if not _usable_section(section):
        return None, False
    return section, True


def _configuration_value(settings: Mapping[str, object], section: str) -> object:
    """The setting the dotted section names, or None when it is absent."""
    current: object = settings
    for component in section.split("."):
        if not component or not isinstance(current, dict):
            return None
        current = current.get(component)
    return current


def configuration_result(settings: Mapping[str, object], item: object) -> object:
    """What to answer for one requested configuration item.

    An item without a section asks for this server's whole configuration, as
    the reference clients answer it (vscode-languageclient, Neovim); it is the
    session's own profile settings, never another profile's.
    """
    section, usable = _configuration_section(item)
    if not usable:
        return None
    if section is None:
        return settings
    return _configuration_value(settings, section)


class SessionProgress:
    """Record server progress and answer the progress gate and configuration requests."""

    def _configuration(self, params: object) -> object:
        settings = self._profile.wire_configuration()
        assert isinstance(settings, dict)
        items = _configuration_items(params)
        if items is None:
            return []
        return [configuration_result(settings, item) for item in items]

    def _work_done_progress_create(self, params: object) -> None:
        """Answer the create request, and remember the token it names.

        Answering is what makes the server send `$/progress` at all -- the
        specification only permits server-initiated progress once the client
        declares `window.workDoneProgress` *and* replies here. Remembering the
        token is what lets a progress-gated profile tell its project load from
        any other progress the server may report.
        """
        self._benign_server_request(params)
        assert isinstance(params, dict)
        token = _progress_token(params.get("token"))
        self._retain_work_done_token(token)

    def _retain_work_done_token(self, token: object) -> None:
        if token is None:
            return
        with self._lock:
            if len(self._work_done_tokens) < _MAX_WORK_DONE_TOKENS:
                self._work_done_tokens.add(token)

    @staticmethod
    def _benign_server_request(params: object) -> None:
        if not isinstance(params, dict):
            raise ValueError("server request params must be an object")
        if len(params) > 16:
            raise ValueError("server request params exceed their bound")
        return None

    @staticmethod
    def _progress_event_bytes(event: tuple[object, ...]) -> int:
        return _PROGRESS_EVENT_BASE_BYTES + sum(
            len(value.encode("utf-8", errors="strict"))
            for value in event
            if isinstance(value, str)
        )

    def _retain_progress(self, event: tuple[object, ...]) -> None:
        event_bytes = self._progress_event_bytes(event)
        if event_bytes > _MAX_PROGRESS_BYTES:
            return
        with self._lock:
            self._progress_events.append(event)
            self._progress_bytes += event_bytes
            while (
                len(self._progress_events) > _MAX_PROGRESS_EVENTS
                or self._progress_bytes > _MAX_PROGRESS_BYTES
            ):
                removed = self._progress_events.pop(0)
                self._progress_bytes -= self._progress_event_bytes(removed)

    def _rearm_progress_gate(self) -> None:
        """A new server process must load the project again before answering."""
        with self._lock:
            self._work_done_tokens.clear()
            self._progress_ready_generation = None

    def _progress(self, method: str, params: object) -> None:
        record = _progress_notification_record(method, params)
        if record is not None:
            self._retain_progress(record)
        self._note_work_done_end(method, params)

    def _note_work_done_end(self, method: str, params: object) -> None:
        """An `end` on a token the server opened closes this generation's load.

        This is the measured difference between a right and a wrong answer:
        ungated, typescript-language-server answers go-to-definition with the
        import binding rather than the declaration -- 0/12 correct against 12/12
        gated. See `docs/research/2026-08-28-precise-navigation-beyond-python.md`,
        Finding 4.
        """
        if method != _NEUTRAL_PROGRESS_METHOD:
            return
        token = _work_done_end_token(params)
        if token is None:
            return
        self._mark_progress_ready(token)

    def _mark_progress_ready(self, token: object) -> None:
        with self._lock:
            if token not in self._work_done_tokens:
                return
            self._progress_ready_generation = self._generation_nonce
            self._condition.notify_all()

    def _progress_gate_satisfied_locked(self) -> bool:
        """Whether this profile's readiness precondition is met right now."""
        if not self._profile.gates_on_progress():
            return True
        generation = self._generation_nonce
        return generation is not None and self._progress_ready_generation == generation

    def _progress_gate_limit_locked(self, deadline: float, grace: float) -> float:
        """How long to wait: a server that opened no token is not loading."""
        if self._work_done_tokens:
            return deadline
        return min(deadline, grace)

    def _await_progress_gate(self, deadline: float) -> None:
        """Block until the project load is declared finished, or the deadline.

        A server that has not asked us to create a work-done token is not
        loading a project: either it does not report progress at all, or it has
        not started. Waiting the caller's whole deadline for a notification that
        is not coming spends the budget the query itself needs, and the answer
        is `not_ready` either way -- so that case waits only a short grace.
        """
        if not self._profile.gates_on_progress():
            return
        grace = time.monotonic() + PROGRESS_GATE_GRACE_SECONDS
        with self._lock:
            self._wait_for_progress_gate_locked(deadline, grace)

    def _wait_for_progress_gate_locked(self, deadline: float, grace: float) -> None:
        while not self._progress_gate_satisfied_locked():
            if not self._waited_for_progress_locked(deadline, grace):
                return

    def _waited_for_progress_locked(self, deadline: float, grace: float) -> bool:
        """Wait one poll for progress; False once the gate's time has run out."""
        limit = self._progress_gate_limit_locked(deadline, grace)
        remaining = limit - time.monotonic()
        if remaining <= 0:
            return False
        self._condition.wait(min(remaining, _PROGRESS_POLL_SECONDS))
        return True

    def _progress_methods(self) -> tuple[str, ...]:
        """`$/progress` plus this profile's own vendor progress notifications."""
        identity = self._profile.identity_notification
        vendor = tuple(
            method
            for method in sorted(self._profile.server_notifications)
            if identity is None or method != identity.method
        )
        return (_NEUTRAL_PROGRESS_METHOD, *vendor)
