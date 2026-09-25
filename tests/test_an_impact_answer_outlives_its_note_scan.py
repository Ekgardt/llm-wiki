"""A failed note scan costs the word-match pages, never the impact answer (audit C-35).

docs/research/2026-09-25-an-impact-answer-outlives-its-note-scan.md
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import impact_analysis  # noqa: E402
from impact_analysis import ImpactLimits, analyze_impact  # noqa: E402


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, capture_output=True, check=True)


def _changed_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "impact@example.test")
    _git(root, "config", "user.name", "Impact Test")
    (root / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    (root / "alpha.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    return root


def _notes(tmp_path: Path, monkeypatch) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    for name in ("a.md", "b.md"):
        (notes / name).write_text("# Page\n\nUses alpha.\n", encoding="utf-8")
    monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)


def _changed_during_scan(*_arguments):
    raise PermissionError("impact note changed after discovery")


def _scan_ceiling(monkeypatch) -> ImpactLimits:
    return ImpactLimits(max_note_files=1)


def _changed_note(monkeypatch) -> ImpactLimits:
    monkeypatch.setattr(impact_analysis, "_note_text", _changed_during_scan)
    return ImpactLimits()


@pytest.mark.parametrize("failure", [_scan_ceiling, _changed_note])
def test_the_computed_answer_survives_a_failed_note_scan(tmp_path, monkeypatch, failure):
    root = _changed_repository(tmp_path)
    _notes(tmp_path, monkeypatch)

    result = analyze_impact(root=root, graph=None, limits=failure(monkeypatch))

    assert result["changed_files"] == ["alpha.py"]
    assert result["textual_fallback"] == []
    assert result["partial"] is True
    assert any(w.startswith("Textual fallback unavailable") for w in result["warnings"])


def test_a_deadline_during_the_scan_still_stops_the_request(tmp_path, monkeypatch):
    root = _changed_repository(tmp_path)
    _notes(tmp_path, monkeypatch)
    monkeypatch.setattr(impact_analysis, "_note_text", _deadline_reached)

    with pytest.raises(TimeoutError):
        analyze_impact(root=root, graph=None)


def _deadline_reached(*_arguments):
    raise TimeoutError("impact analysis deadline reached")
