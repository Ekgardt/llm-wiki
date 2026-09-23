"""The adoption gate says what it saw, and the queue waits out a busy database.

See `docs/research/2026-09-23-the-adoption-gate-names-its-cause-and-waits-out-contention.md`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import installed_memory_repair  # noqa: E402
import markdown_transaction  # noqa: E402
import memory_queue  # noqa: E402

from tests.test_doctor import _adopt, _build_root  # noqa: E402


def test_an_invalid_record_names_the_cause_behind_its_code(tmp_path: Path) -> None:
    root, state_root, _home = _build_root(tmp_path)
    _adopt(root, state_root)
    (state_root / "run" / "reliability-v3-adopted.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(installed_memory_repair.ReliabilityV3ValidationError) as raised:
        installed_memory_repair.require_reliability_v3_adopted(root=root, state_root=state_root)

    assert raised.value.code == "reliability_v3_record_invalid"
    assert raised.value.detail and raised.value.detail != raised.value.code
    assert str(raised.value).startswith("reliability_v3_record_invalid: ")
    assert raised.value.__cause__ is not None


def test_a_code_without_detail_reads_as_before() -> None:
    error = installed_memory_repair.ReliabilityV3ValidationError("legacy_protocol_unquiesced")

    assert (str(error), error.code, error.detail) == ("legacy_protocol_unquiesced", "legacy_protocol_unquiesced", "")


def test_the_adopted_queue_waits_out_a_busy_database(tmp_path: Path, monkeypatch) -> None:
    root, state_root, _home = _build_root(tmp_path)
    _adopt(root, state_root)
    monkeypatch.setattr(markdown_transaction, "_ADOPTION_VALIDATION_CACHE", set())
    real = installed_memory_repair.require_reliability_v3_adopted
    calls: list[int] = []

    def busy_once(*, root, state_root):
        calls.append(1)
        if len(calls) == 1:
            raise installed_memory_repair.ReliabilityV3ValidationError(
                "reliability_v3_record_invalid", "OperationalError: database is locked"
            ) from sqlite3.OperationalError("database is locked")
        return real(root=root, state_root=state_root)

    monkeypatch.setattr(installed_memory_repair, "require_reliability_v3_adopted", busy_once)

    queue = memory_queue.active_memory_queue(root, state_root)

    assert queue is not None
    assert len(calls) == 2
