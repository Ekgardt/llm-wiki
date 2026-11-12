"""One proof parses one immutable layout; its final live-source guard remains."""
import time

import pytest
from llm_wiki import compile_pipeline_sources, doctor_transactions

from tests.compile_paths import point_compile_path
from tests.doctor_current_v4_exact_historical_work_helpers import exact_work as exact_work
from tests.doctor_exact_legacy_source_outcome_helpers import _reader
from tests.historical_work_captures_each_physical_layout_once_helpers import _published_units


def _record_parses(monkeypatch):
    calls = []
    original = compile_pipeline_sources.native_daily_frames

    def observed(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    point_compile_path(monkeypatch, "native_daily_frames", observed)
    return calls


def test_complete_two_unit_proof_parses_one_physical_layout(exact_work, monkeypatch):
    history, parts = _published_units(exact_work)
    calls = _record_parses(monkeypatch)
    with _reader(history) as (_database, reader):
        assert doctor_transactions.CurrentHistoricalSourceWork(reader, history[4]).verified()
    assert calls == [parts[0].logical_path]


def test_separate_proofs_capture_separate_layouts(exact_work, monkeypatch):
    history, parts = _published_units(exact_work)
    calls = _record_parses(monkeypatch)
    with _reader(history) as (_database, reader):
        assert doctor_transactions.CurrentHistoricalSourceWork(reader, history[4]).verified()
        assert doctor_transactions.CurrentHistoricalSourceWork(reader, history[4]).verified()
    assert calls == [parts[0].logical_path, parts[0].logical_path]


def test_layout_reuse_still_honors_expired_deadline(exact_work):
    history, parts = _published_units(exact_work)
    with (
        _reader(history) as (_database, reader),
        doctor_transactions.CurrentHistoricalSourceWork(reader, history[4]) as work,
    ):
        work._capture(parts[0].logical_path)
        reader.deadline = time.monotonic() - 1
        with pytest.raises(TimeoutError):
            work._capture(parts[0].logical_path)
