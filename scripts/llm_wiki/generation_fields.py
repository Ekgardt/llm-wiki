"""Generation identifiers, versions, clocks and deadlines."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from iso_time import utc_text
from llm_wiki.generation_limits import GENERATION_RE, WINDOWS_RESERVED
from llm_wiki.sqlite_deadlines import busy_database


def _safe_path_component(value: str) -> bool:
    if GENERATION_RE.fullmatch(value) is None:
        return False
    return value not in {".", ".."} and value[-1] not in {".", " "}


def checked_generation_id(value: object) -> str:
    if not isinstance(value, str) or not _safe_path_component(value):
        raise ValueError("generation_id must be an exact safe path component")
    if value.split(".", 1)[0].casefold() in WINDOWS_RESERVED:
        raise ValueError("generation_id is a reserved path component")
    return value


def _bounded_version_text(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 128:
        return False
    return not any(character in value for character in "\x00\r\n")


def bounded_version(name: str, value: object) -> str:
    if not _bounded_version_text(value):
        raise ValueError(f"{name} must be a bounded non-empty string")
    try:
        str(value).encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid Unicode") from exc
    return str(value)


def optional_version(name: str, value: object) -> str | None:
    if value is None:
        return None
    return bounded_version(name, value)


def _parsed_clock_text(value: str) -> datetime:
    if len(value) > 40 or any(character in value for character in "\x00\r\n"):
        raise ValueError("clock returned an invalid timestamp")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("clock must return an ISO-8601 timestamp") from exc


def _parsed_clock(value: object) -> datetime:
    if isinstance(value, str):
        return _parsed_clock_text(value)
    if isinstance(value, datetime):
        return value
    raise TypeError("clock must return a datetime or ISO-8601 string")


def utc_timestamp(clock: Callable[[], datetime | str]) -> str:
    parsed = _parsed_clock(clock())
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("clock timestamp must include a UTC offset")
    return utc_text(parsed)


def check_monotonic_deadline(deadline: float | None, monotonic: Callable[[], float]) -> None:
    if deadline is not None and monotonic() >= deadline:
        raise TimeoutError("generation catalog deadline reached")


def check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if bool(cancelled and cancelled()):
        raise TimeoutError("generation catalog operation cancelled")


def raise_writer_timeout(exc: sqlite3.OperationalError, deadline: float | None) -> None:
    if deadline is not None and busy_database(exc):
        raise TimeoutError("generation catalog writer deadline reached") from exc


def require_catalog_bound(max_catalog_bytes: object) -> None:
    if isinstance(max_catalog_bytes, bool) or not isinstance(max_catalog_bytes, int):
        raise ValueError("max_catalog_bytes must be a positive integer")
    if max_catalog_bytes < 1:
        raise ValueError("max_catalog_bytes must be a positive integer")


def require_inside_state_root(catalog_path: Path, state_root: Path) -> None:
    try:
        catalog_path.relative_to(state_root)
    except ValueError as exc:
        raise ValueError("catalog_path must remain inside state_root") from exc


def valid_page_metric(value: object, minimum: int) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value >= minimum


def _finite_number(value: object) -> bool:
    """A real finite number; a bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def require_absolute_deadline(deadline: object) -> None:
    if deadline is None:
        return
    if not _finite_number(deadline):
        raise ValueError("deadline must be an absolute monotonic timestamp or None")
