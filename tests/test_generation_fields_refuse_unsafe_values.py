from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from llm_wiki import generation_fields


@pytest.mark.parametrize("reserved", ["con", "NUL.db", "com1", "Lpt9.sqlite3"])
def test_a_windows_reserved_name_is_not_a_generation_id(reserved: str) -> None:
    with pytest.raises(ValueError, match="reserved path component"):
        generation_fields.checked_generation_id(reserved)


@pytest.mark.parametrize("unsafe", ["", "..", "name.", "name ", "-leading", 7, "a/b"])
def test_an_unsafe_component_is_not_a_generation_id(unsafe: object) -> None:
    with pytest.raises(ValueError, match="exact safe path component"):
        generation_fields.checked_generation_id(unsafe)


def test_a_safe_generation_id_is_returned_unchanged() -> None:
    assert generation_fields.checked_generation_id("gen-2026.11_17") == "gen-2026.11_17"


def test_a_version_holding_a_lone_surrogate_is_not_valid_unicode() -> None:
    lone_surrogate = "v1" + chr(0xD800)
    with pytest.raises(ValueError, match="collector_version must be valid Unicode"):
        generation_fields.bounded_version("collector_version", lone_surrogate)


@pytest.mark.parametrize("value", ["", "x" * 129, "line\nbreak", None, 3])
def test_an_unbounded_version_is_refused_by_name(value: object) -> None:
    with pytest.raises(ValueError, match="schema_version must be a bounded non-empty string"):
        generation_fields.bounded_version("schema_version", value)


def test_an_absent_optional_version_stays_absent() -> None:
    assert generation_fields.optional_version("revision", None) is None
    assert generation_fields.optional_version("revision", "r1") == "r1"


def _clock_returning(value: datetime | str) -> Callable[[], datetime | str]:
    def clock() -> datetime | str:
        return value

    return clock


@pytest.mark.parametrize(
    ("returned", "message"),
    [
        ("2026-11-17T10:00:00+00:00" + "0" * 30, "clock returned an invalid timestamp"),
        ("2026-11-17\n", "clock returned an invalid timestamp"),
        ("yesterday", "clock must return an ISO-8601 timestamp"),
        ("2026-11-17T10:00:00", "clock timestamp must include a UTC offset"),
    ],
)
def test_a_clock_text_that_is_not_an_offset_timestamp_is_refused(
    returned: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        generation_fields.utc_timestamp(_clock_returning(returned))


def test_a_clock_returning_neither_text_nor_datetime_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="datetime or ISO-8601 string"):
        generation_fields.utc_timestamp(cast(Callable[[], datetime | str], lambda: 1700000000))


def test_a_clock_text_with_an_offset_is_stored_as_utc() -> None:
    stored = generation_fields.utc_timestamp(_clock_returning("2026-11-17T12:00:00+02:00"))
    assert stored == "2026-11-17T10:00:00.000000Z"


def test_a_clock_datetime_is_stored_as_utc() -> None:
    moment = datetime(2026, 11, 17, 10, 0, 0, 500000, tzinfo=UTC)
    assert generation_fields.utc_timestamp(_clock_returning(moment)) == (
        "2026-11-17T10:00:00.500000Z"
    )


def test_a_busy_writer_with_a_deadline_becomes_a_timeout() -> None:
    busy = sqlite3.OperationalError("database is locked")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    with pytest.raises(TimeoutError, match="writer deadline reached") as raised:
        generation_fields.raise_writer_timeout(busy, 10.0)
    assert raised.value.__cause__ is busy


def test_a_busy_writer_without_a_deadline_is_left_to_the_caller() -> None:
    busy = sqlite3.OperationalError("database is locked")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    generation_fields.raise_writer_timeout(busy, None)


@pytest.mark.parametrize("bound", [True, 0, -1, "10", 1.5])
def test_a_catalog_bound_must_be_a_positive_integer(bound: object) -> None:
    with pytest.raises(ValueError, match="max_catalog_bytes must be a positive integer"):
        generation_fields.require_catalog_bound(bound)


def test_a_catalog_outside_the_state_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must remain inside state_root"):
        generation_fields.require_inside_state_root(tmp_path / "elsewhere", tmp_path / "state")
    generation_fields.require_inside_state_root(tmp_path / "state" / "c.db", tmp_path / "state")


@pytest.mark.parametrize(
    ("value", "minimum", "valid"),
    [(True, 0, False), ("4", 0, False), (3, 4, False), (4, 4, True)],
)
def test_a_page_metric_is_an_integer_at_least_its_minimum(
    value: object, minimum: int, valid: bool
) -> None:
    assert generation_fields.valid_page_metric(value, minimum) is valid


@pytest.mark.parametrize("deadline", [True, "5", float("nan"), float("inf")])
def test_a_deadline_must_be_a_finite_number(deadline: object) -> None:
    with pytest.raises(ValueError, match="absolute monotonic timestamp or None"):
        generation_fields.require_absolute_deadline(deadline)


@pytest.mark.parametrize("deadline", [None, 12, 12.5])
def test_an_absent_or_finite_deadline_is_accepted(deadline: object) -> None:
    generation_fields.require_absolute_deadline(deadline)


def test_a_passed_catalog_deadline_or_a_cancellation_stops_the_operation() -> None:
    with pytest.raises(TimeoutError, match="deadline reached"):
        generation_fields.check_monotonic_deadline(10.0, lambda: 10.0)
    with pytest.raises(TimeoutError, match="operation cancelled"):
        generation_fields.check_cancelled(lambda: True)
    generation_fields.check_monotonic_deadline(None, lambda: 10.0)
    generation_fields.check_cancelled(None)
