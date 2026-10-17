"""A stored instant's text order is its time order (audit 2026-09-26 C-12).

docs/research/2026-09-26-every-stored-instant-has-one-width.md
"""
from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

WHOLE = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("module_name", ["iso_time", "markdown_transaction", "project_journal"])
def test_a_whole_second_sorts_before_the_half_second_after_it(module_name) -> None:
    import importlib

    module = importlib.import_module(module_name)
    write = getattr(module, "utc_text", None) or module._timestamp

    earlier, later = write(WHOLE), write(WHOLE + timedelta(milliseconds=500))

    assert (earlier, earlier < later) == ("2026-09-26T10:00:00.000000Z", True)



_SQL_TIME_COMPARISON = re.compile(r"_at\s*(?:<=|>=|<|>)\s*\?")


def _is_calendar_date(node: ast.AST) -> bool:
    """`date.fromisoformat(...)`: a day has no fraction to drop."""
    return ast.unparse(node).startswith("date.fromisoformat(")


def _bare_isoformat(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "isoformat" or node.keywords:
        return False
    return not _is_calendar_date(node.func.value)


def _bare_isoformat_lines(tree: ast.AST) -> list[int]:
    """`x.isoformat()` with no `timespec`, on anything but a calendar date."""
    return [node.lineno for node in ast.walk(tree) if _bare_isoformat(node)]


def test_a_module_that_compares_times_in_sql_writes_them_in_one_width() -> None:
    offenders = {}
    for path in sorted((Path(__file__).resolve().parents[1] / "scripts").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if _SQL_TIME_COMPARISON.search(source):
            offenders[path.name] = _bare_isoformat_lines(ast.parse(source))

    assert {name: lines for name, lines in offenders.items() if lines} == {}
    assert "markdown_transaction.py" in offenders


# The microsecond `Z` form `utc_text` owns; `trace_ingest` keeps its own
# seconds-wide stamps in its own table, a different format on purpose.
_WRITER_SHAPE = re.compile(r"\.isoformat\(timespec='microseconds'\)\.replace$")


def _hand_rolled_lines(path: Path) -> list[int]:
    """Lines spelling `x.isoformat(timespec=...).replace(...)`: `iso_time.utc_text` written again."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    return sorted({node.lineno for node in calls if _WRITER_SHAPE.search(ast.unparse(node.func))})


def test_one_module_writes_the_fixed_width_instant() -> None:
    """Four copies of the writer drifted apart (audit 2026-09-27 C-18); only iso_time may hold it."""
    scripts = sorted((Path(__file__).resolve().parents[1] / "scripts").glob("*.py"))
    copies = {path.name: _hand_rolled_lines(path) for path in scripts if path.name != "iso_time.py"}

    assert {name: lines for name, lines in copies.items() if lines} == {}


def _adapter_event(at):
    from event_envelope import build_event_envelope

    return build_event_envelope(event_type="session_end", payload={"reason": "clear", "transcript_path": None},
                                occurred_at=at, captured_at=at, agent="codex", session="clock-test")


def _later_clock(at):
    return at + timedelta(hours=1)


@pytest.mark.parametrize("at", [WHOLE, WHOLE + timedelta(microseconds=500001)])
def test_adapter_pending_clock_keeps_width_and_actual_reducer_roundtrip(at):
    import integration_adapter as adapter

    envelope = _adapter_event(at)
    pending = adapter._pending_checkpoint(envelope, "demo", "demo:clock-test")
    assert pending["occurred_at"] == at.isoformat(timespec="microseconds")
    decision = adapter._observe_pending_item(pending, {}, {})
    assert decision is not None and decision.checkpoint_at == at


@pytest.mark.parametrize("at", [WHOLE, WHOLE + timedelta(microseconds=500001)])
def test_adapter_inflight_clock_keeps_width_and_recovery_roundtrip(at):
    import integration_adapter as adapter
    from project_journal import CheckpointDecision

    pending = adapter._pending_checkpoint(_adapter_event(at), "demo", "demo:clock-test")
    pending["claim_owner"] = "clock-owner"
    state = {"project_checkpoint_pending": {"demo": [pending]}}
    adapter._record_inflight_state(state, "demo", "clock-owner", [pending],
                                   CheckpointDecision("session_end", checkpoint_at=at))
    inflight = state[adapter.INFLIGHT_STATE_KEY]["demo"]
    assert inflight["checkpoint_at"] == at.isoformat(timespec="microseconds")
    replayed = adapter._inflight_plan([pending], {}, inflight)
    assert replayed is not None and replayed[-1].checkpoint_at == at


@pytest.mark.parametrize("at", [WHOLE, WHOLE + timedelta(microseconds=500001)])
def test_adapter_capture_clock_keeps_width_and_session_filing_roundtrip(at):
    import flush_memory
    import integration_adapter as adapter

    record = adapter._capture_source_record(_adapter_event(at), "demo", "clear", "source")
    assert record["occurred_at"] == at.isoformat(timespec="microseconds")
    assert flush_memory._session_time(record, partial(_later_clock, at)) == at


def test_adapter_clock_readers_keep_historical_width_and_absent_clock():
    import flush_memory
    import integration_adapter as adapter

    assert adapter._inflight_time({"checkpoint_at": WHOLE.isoformat()}) == WHOLE
    assert flush_memory._intent_time({"occurred_at": WHOLE.isoformat()}) == WHOLE
    assert adapter._inflight_time({"checkpoint_at": None}) is None
    assert adapter._capture_occurred_at(SimpleNamespace(occurred_at=None)) is None


def test_adapter_capture_clock_preserves_an_existing_offset():
    import integration_adapter as adapter

    at = WHOLE.astimezone(timezone(timedelta(hours=5, minutes=30)))
    encoded = adapter._capture_occurred_at(SimpleNamespace(occurred_at=at))
    assert encoded == at.isoformat(timespec="microseconds")
    assert datetime.fromisoformat(encoded) == WHOLE
