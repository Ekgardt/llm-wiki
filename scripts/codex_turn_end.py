"""When the end of a Codex turn is also a capture of its session.

Codex fires `Stop` once per turn. Treating each one as the end of the session
asked the classifier about the same growing transcript thirty times in a
thirty-turn session. A turn end is a capture only when its session has not been
captured within the window; otherwise the session is remembered as having an
uncaptured tail, and the next Codex hook that runs after the tail has been quiet
for the window captures it.

The bookkeeping is one bounded map in `run/state.json`. See
`docs/research/2026-09-17-a-codex-turn-is-not-a-session.md`.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

STATE_KEY = "codex_turn_end_captures"
# The host's own figure: Codex ends a session that has been idle for 30 minutes.
CAPTURE_WINDOW_SECONDS = 30 * 60
MAX_SESSIONS = 64
TAIL_FIELDS = ("cwd", "transcript_path", "turn_id")


def _age_seconds(stamp: object, now: datetime) -> float:
    """How long ago; a stamp that cannot be read is infinitely old."""
    if not isinstance(stamp, str):
        return math.inf
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds()
    except (ValueError, TypeError):
        return math.inf


def _sessions(state: dict[str, Any]) -> dict[str, Any]:
    sessions = state.get(STATE_KEY)
    if not isinstance(sessions, dict):
        sessions = {}
        state[STATE_KEY] = sessions
    return sessions


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _last_touch(entry: object) -> str:
    """The newest stamp an entry carries, for choosing which sessions to keep."""
    known = _mapping(entry)
    tail_at = _mapping(known.get("pending")).get("at")
    return max(str(known.get("captured_at") or ""), str(tail_at or ""))


def _trim(state: dict[str, Any]) -> None:
    sessions = _sessions(state)
    if len(sessions) <= MAX_SESSIONS:
        return
    newest = sorted(sessions, key=lambda name: _last_touch(sessions[name]), reverse=True)
    state[STATE_KEY] = {name: sessions[name] for name in newest[:MAX_SESSIONS]}


def mark_captured(state: dict[str, Any], session_id: str, now: datetime) -> None:
    """This session was captured whole just now; it has no tail."""
    _sessions(state)[session_id] = {"captured_at": now.isoformat()}
    _trim(state)


def _tail(raw: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    tail = {name: raw.get(name) for name in TAIL_FIELDS}
    tail["at"] = now.isoformat()
    return tail


def claim_turn_end(state: dict[str, Any], raw: Mapping[str, Any], now: datetime) -> bool:
    """True when this turn end is a capture; otherwise the turn becomes the tail."""
    session_id = str(raw["session_id"])
    captured_at = _mapping(_sessions(state).get(session_id)).get("captured_at")
    if _age_seconds(captured_at, now) >= CAPTURE_WINDOW_SECONDS:
        mark_captured(state, session_id, now)
        return True
    _sessions(state)[session_id] = {"captured_at": captured_at, "pending": _tail(raw, now)}
    _trim(state)
    return False


def _quiet_tail(entry: object, now: datetime) -> Mapping[str, Any] | None:
    pending = _mapping(entry).get("pending")
    if not isinstance(pending, Mapping):
        return None
    if _age_seconds(pending.get("at"), now) < CAPTURE_WINDOW_SECONDS:
        return None
    return pending


def claim_quiet_tail(
    state: dict[str, Any], now: datetime, *, except_session: str
) -> dict[str, Any] | None:
    """One session whose tail has been quiet for the window, claimed as captured.

    `before` is the entry as it stood, for `restore` when the capture fails.
    """
    sessions = _sessions(state)
    for session_id in sorted(sessions):
        before = sessions[session_id]
        tail = _quiet_tail(before, now)
        if tail is not None and session_id != except_session:
            mark_captured(state, session_id, now)
            return {**tail, "session_id": session_id, "before": before}
    return None


def restore(state: dict[str, Any], session_id: str, entry: object) -> None:
    """Put a claim back after the capture it stood for failed."""
    if entry is None:
        _sessions(state).pop(session_id, None)
        return
    _sessions(state)[session_id] = entry


def snapshot(state: Mapping[str, Any], session_id: str) -> object:
    """The entry as it stands, to hand to `restore` later."""
    sessions = state.get(STATE_KEY)
    if not isinstance(sessions, Mapping):
        return None
    return sessions.get(session_id)
