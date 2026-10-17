"""One proof parses one immutable layout; its final live-source guard remains."""
import time

import compile_memory as compiler
import doctor
import pytest

from tests.test_doctor_current_v4_exact_historical_work import (
    _publish_multi_current,
    _reader,
    _two_current_records,
    _two_source_history,
)
from tests.test_doctor_current_v4_exact_historical_work import (
    exact_work as exact_work,
)


def _published_units(exact_work):
    history, parts = _two_source_history(exact_work)
    _publish_multi_current(history, _two_current_records(history[4], parts))
    return history, parts


def _record_parses(monkeypatch):
    calls = []
    original = compiler._native_daily_frames

    def observed(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler, "_native_daily_frames", observed)
    return calls


def test_complete_two_unit_proof_parses_one_physical_layout(exact_work, monkeypatch):
    history, parts = _published_units(exact_work)
    calls = _record_parses(monkeypatch)
    with _reader(history) as (_database, reader):
        assert doctor._CurrentHistoricalSourceWork(reader, history[4]).verified()
    assert calls == [parts[0].logical_path]


def test_separate_proofs_capture_separate_layouts(exact_work, monkeypatch):
    history, parts = _published_units(exact_work)
    calls = _record_parses(monkeypatch)
    with _reader(history) as (_database, reader):
        assert doctor._CurrentHistoricalSourceWork(reader, history[4]).verified()
        assert doctor._CurrentHistoricalSourceWork(reader, history[4]).verified()
    assert calls == [parts[0].logical_path, parts[0].logical_path]


def test_layout_reuse_still_honors_expired_deadline(exact_work):
    history, parts = _published_units(exact_work)
    with _reader(history) as (_database, reader):
        work = doctor._CurrentHistoricalSourceWork(reader, history[4])
        work._capture(parts[0].logical_path)
        reader.deadline = time.monotonic() - 1
        with pytest.raises(TimeoutError):
            work._capture(parts[0].logical_path)
