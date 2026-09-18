"""Inputs the queue used to accept and then crash on are refused or handled.

A source-failure code between 65 and 200 characters passed validation and broke
the insert's CHECK; a payload with a non-string key raised `AttributeError` out
of the redaction walk; a long redrive chain ordered itself with Python's stack.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402
from memory_queue import MemoryQueue  # noqa: E402

_TOO_LONG = "e" * 65
_CYRILLIC = "ошибка" * 8  # 48 characters, 96 bytes.


def _v3_queue(tmp_path: Path):
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    memory_queue.initialize_queue_v3_candidate(candidate, source_v2=None)
    return MemoryQueue._from_v3_candidate(candidate, state_root=tmp_path)


def _refused(queue, error_code: str) -> str:
    with pytest.raises(ValueError) as raised:
        queue.record_source_failure(
            logical_path="knowledge/daily/2026-09-18.md",
            source_digest="0" * 64,
            error_code=error_code,
            producer="compile",
        )
    return str(raised.value)


def test_a_code_the_schema_cannot_store_is_refused_by_name(tmp_path: Path) -> None:
    queue = _v3_queue(tmp_path)

    assert (_refused(queue, _TOO_LONG), _refused(queue, _CYRILLIC)) == (
        "source failure fields are invalid",
        "source failure fields are invalid",
    )


def test_a_code_that_fits_in_its_bytes_is_stored(tmp_path: Path) -> None:
    queue = _v3_queue(tmp_path)

    queue.record_source_failure(
        logical_path="knowledge/daily/2026-09-18.md",
        source_digest="0" * 64,
        error_code="e" * 64,
        producer="compile",
    )

    with sqlite3.connect(queue.db_path) as database:
        stored = database.execute("SELECT error_code FROM source_failures").fetchone()
    assert stored == ("e" * 64,)


def test_a_payload_key_that_is_not_a_string_is_refused_by_its_own_rule(
    tmp_path: Path,
) -> None:
    """The canonical encoder refuses it; the redaction walk no longer breaks first."""
    queue = _v3_queue(tmp_path)

    with pytest.raises(TypeError) as raised:
        queue.enqueue("query", 1, {1: "plain", "token": "secret-value"})

    assert str(raised.value) == "canonical JSON object keys must be strings"


def test_the_redaction_walk_passes_a_non_string_key_to_the_encoder() -> None:
    walked = memory_queue._redact_payload({1: "plain", "token": "secret-value"})

    assert walked == {1: "plain", "token": "[REDACTED]"}


def test_a_long_redrive_chain_orders_without_python_s_stack() -> None:
    """The deepest child sorts first, so the walk meets the whole chain at once."""
    depth = sys.getrecursionlimit() * 2
    last = depth - 1
    rows = {
        f"task-{index:06d}": {
            "id": f"task-{index:06d}",
            "redrive_of": None if index == last else f"task-{index + 1:06d}",
        }
        for index in range(depth)
    }

    ordered = memory_queue._parents_before_children(rows)

    # Parents first: the oldest ancestor is the highest-numbered task.
    assert [row["id"] for row in ordered] == [
        f"task-{index:06d}" for index in range(last, -1, -1)
    ]


def test_a_cycle_in_the_chain_is_still_named(tmp_path: Path) -> None:
    rows = {
        "a": {"id": "a", "redrive_of": "b"},
        "b": {"id": "b", "redrive_of": "a"},
    }

    with pytest.raises(memory_queue.OperationalDatabaseContractError) as raised:
        memory_queue._parents_before_children(rows)

    assert raised.value.code == "queue_v2_lineage_ambiguous"
