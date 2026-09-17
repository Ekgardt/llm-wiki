"""A damaged generation is named as damaged, and a wrong argument is not hidden.

Audit 3, K-B26. Research:
`docs/research/2026-09-17-an-unreadable-generation-is-not-a-missing-one.md`.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tests.test_impact_analysis import _repository  # noqa: E402


def _state_with_catalog(tmp_path: Path, content: bytes) -> Path:
    state = tmp_path / "state"
    catalog = state / "cache" / "evidence-graph" / "catalog.sqlite3"
    catalog.parent.mkdir(parents=True)
    catalog.write_bytes(content)
    return state


def _changed_repository(tmp_path: Path) -> Path:
    root = _repository(tmp_path)
    (root / "alpha.py").write_bytes(b"def alpha():\n    return 200\n")
    return root


def test_a_corrupt_catalog_is_reported_as_unreadable_not_as_no_generation(
    tmp_path, monkeypatch
):
    import impact_analysis

    root = _changed_repository(tmp_path)
    monkeypatch.setattr(
        impact_analysis, "STATE_ROOT", _state_with_catalog(tmp_path, b"not a database")
    )

    with pytest.raises(impact_analysis.GenerationUnreadable):
        impact_analysis._active_graph(root, time.monotonic() + 5)


def test_the_analysis_names_the_damage_in_its_warnings(tmp_path, monkeypatch):
    import impact_analysis

    root = _changed_repository(tmp_path)
    monkeypatch.setattr(
        impact_analysis, "STATE_ROOT", _state_with_catalog(tmp_path, b"not a database")
    )

    report = impact_analysis.analyze_impact(root=root, textual_fallback=False)

    named = [warning for warning in report["warnings"] if "unreadable" in warning]
    assert (report["classification"], len(named)) == ("unresolved", 1)


def test_a_wrong_argument_is_not_reported_as_a_missing_generation(
    tmp_path, monkeypatch
):
    import impact_analysis

    def wrong_argument(*_args, **_options):
        raise TypeError("resolve_repository_scope() got an unexpected keyword")

    monkeypatch.setattr(
        impact_analysis, "STATE_ROOT", _state_with_catalog(tmp_path, b"")
    )
    monkeypatch.setattr(impact_analysis, "_opened_active_graph", wrong_argument)

    with pytest.raises(TypeError):
        impact_analysis._active_graph(tmp_path, time.monotonic() + 5)


def test_a_generation_that_keeps_moving_is_unreadable(tmp_path, monkeypatch):
    import impact_analysis

    def changing(*_args, **_options):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(
        impact_analysis, "STATE_ROOT", _state_with_catalog(tmp_path, b"")
    )
    monkeypatch.setattr(impact_analysis, "_opened_active_graph", changing)

    with pytest.raises(impact_analysis.GenerationUnreadable, match="malformed"):
        impact_analysis._active_graph(tmp_path, time.monotonic() + 5)
