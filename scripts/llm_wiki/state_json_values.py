"""JSON state values with an explicit caller-owned byte budget."""
from __future__ import annotations

import errno
import json
from typing import Any

# The maps that grow with use: dedupe memory and per-project reducer caches.
# Entries have varying encoded sizes; the shared byte budget bounds the file.
_TRIMMABLE_STATE_KEYS = (
    "tool_capture_dedupe",
    "prompt_capture_dedupe",
    "project_checkpoint_reducers",
)


def _decoded_state(raw: bytes) -> dict[str, Any] | None:
    """The state these bytes hold, or None: not UTF-8, not JSON, or not an object.

    One definition of "readable" for the reader and the writer. See
    `docs/research/2026-09-17-a-torn-state-file-is-recovered-whatever-its-bytes.md`.
    """
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _state_bytes(state: dict[str, Any]) -> int:
    return len(json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8"))


def _evict_oldest(state: dict[str, Any], key: str) -> bool:
    """Drop the oldest entry of one growth map; True when something went."""
    entries = state.get(key)
    if not isinstance(entries, dict) or not entries:
        return False
    entries.pop(next(iter(entries)))
    return True


def trim_state_to_budget(state: dict[str, Any], target_bytes: int) -> int:
    """Evict oldest dedupe and reducer entries until the file fits its bound.

    Returns how many entries were dropped. Losing a dedupe entry can cost one
    duplicate capture later; losing the health of two checks costs every finding
    they would have made, which is the worse of the two.
    """
    dropped = 0
    while _state_bytes(state) > target_bytes:
        if not any(_evict_oldest(state, key) for key in _TRIMMABLE_STATE_KEYS):
            return dropped
        dropped += 1
    return dropped


def _serialized_state(state: dict[str, Any], target_bytes: int) -> str:
    trim_state_to_budget(state, target_bytes)
    text = json.dumps(state, indent=2, ensure_ascii=False)
    if len(text.encode("utf-8")) > target_bytes:
        raise OSError(errno.EFBIG, "state exceeds the shared writable byte budget; protected state was preserved")
    return text
