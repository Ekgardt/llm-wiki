"""A historical verdict cannot outlive changed source evidence."""
import weakref

import compile_receipts
import pytest
from bounded_io import SourceChangedDuringRead
from llm_wiki import doctor_transactions

from tests.compile_paths import point_compile_path
from tests.doctor_current_v4_exact_historical_work_helpers import exact_work as exact_work
from tests.doctor_exact_legacy_source_outcome_helpers import _reader
from tests.historical_work_captures_each_physical_layout_once_helpers import _published_units


def test_source_change_after_final_witness_cannot_return_success(exact_work, monkeypatch):
    history, parts = _published_units(exact_work)
    path = history[0] / parts[0].logical_path
    original_bytes = path.read_bytes()
    with _reader(history) as (_database, reader):
        work = doctor_transactions.CurrentHistoricalSourceWork(reader, history[4])
        witness = work._witnesses_hold
        calls = []
        sources = []

        def changed_after_witness(*args):
            result = witness(*args)
            calls.append(True)
            if len(calls) == 2:
                sources.extend(work.captured.values())
                path.write_bytes(original_bytes + b"\nchanged")
            return result

        monkeypatch.setattr(work, "_witnesses_hold", changed_after_witness)
        with pytest.raises(SourceChangedDuringRead, match="changed"):
            work.verified()
        assert sources
        assert all(source.stream.closed for source in sources)
        assert work.owner is None
        assert not work.captured


def test_historical_verification_never_reads_whole_daily(exact_work, monkeypatch):
    history, parts = _published_units(exact_work)
    path = history[0] / parts[0].logical_path
    read = doctor_transactions.read_stable_bytes

    def refuse_daily(target, *args, **kwargs):
        if target == path:
            raise AssertionError("historical proof read the entire daily")
        return read(target, *args, **kwargs)

    monkeypatch.setattr(doctor_transactions, "read_stable_bytes", refuse_daily)
    with _reader(history) as (_database, reader):
        work = doctor_transactions.CurrentHistoricalSourceWork(reader, history[4])
        assert work.verified()
        assert not work.captured
        assert not work.layouts
        assert not work.units
        assert work.verified()


def test_layout_releases_unselected_parts_and_closes_its_source(exact_work):
    history, parts = _published_units(exact_work)
    with _reader(history) as (_database, reader):
        with doctor_transactions.CurrentHistoricalSourceWork(reader, history[4]) as work:
            layout = work._capture(parts[0].logical_path)
            records = iter(layout)
            first = next(records)
            source = first.original_content
            reference = weakref.ref(first)
            del first
            next(records)
            assert reference() is None
        with pytest.raises(ValueError, match="closed"):
            next(iter(layout))
        assert source.stream.closed


def test_selected_historical_unit_retains_coordinates_not_part_payloads(exact_work, monkeypatch):
    history, _parts = _published_units(exact_work)
    observed = []
    original = compile_receipts.daily_part_record

    def tracked(*args):
        part = original(*args)
        observed.append(weakref.ref(part))
        return part

    point_compile_path(monkeypatch, "daily_part_record", tracked)
    with _reader(history) as (_database, reader):
        with doctor_transactions.CurrentHistoricalSourceWork(reader, history[4]) as work:
            unit = work._matching_unit(history[4]["batch_manifest"][0])
            assert len(unit)
            assert observed
            assert all(reference() is None for reference in observed)
