"""A database busy past its timeout repeats the append attempt instead of failing it (audit 2026-09-26 B-27).

docs/research/2026-09-26-a-busy-append-repeats-its-attempt.md
"""
from __future__ import annotations

import sqlite3

import markdown_transaction
from markdown_transaction import MarkdownCoordinator

from tests.test_append_race_lineage import _DAILY, _append, state_root, vault  # noqa: F401


def _locked_once(real):
    calls = []

    def once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    return once


def test_a_locked_attempt_is_repeated_and_the_line_lands(vault, state_root, monkeypatch) -> None:  # noqa: F811
    coordinator = MarkdownCoordinator(vault, state_root)
    monkeypatch.setattr(markdown_transaction, "_run_append_candidate", _locked_once(markdown_transaction._run_append_candidate))

    _append(coordinator, "post-tool:busy", b"line-busy\n")

    assert "line-busy" in (vault / _DAILY).read_text(encoding="utf-8")


def test_the_lineage_read_waits_out_a_busy_database(vault, state_root, monkeypatch) -> None:  # noqa: F811
    coordinator = MarkdownCoordinator(vault, state_root)
    monkeypatch.setattr(coordinator, "_record_for_operation_id", _locked_once(coordinator._record_for_operation_id))

    assert markdown_transaction._refused_parent(coordinator, "never-written") is None
