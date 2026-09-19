"""The session-start impact advisory names the pages the graph proved, within a budget.

Audit 3, K-B30: the block ran the whole analysis on every session start and could
print nothing. Research:
`docs/research/2026-09-17-the-session-start-advisory-prints-what-the-graph-proved.md`.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tests.test_impact_analysis import _Graph, _repository  # noqa: E402


def _changed_repository(tmp_path: Path) -> Path:
    root = _repository(tmp_path)
    (root / "alpha.py").write_bytes(b"def alpha():\n    return 200\n")
    return root


def test_a_change_the_graph_maps_to_a_page_is_printed(tmp_path):
    from impact_analysis import analyze_impact, format_for_advisory

    impact = analyze_impact(root=_changed_repository(tmp_path), graph=_Graph(), textual_fallback=False)
    advisory = format_for_advisory(impact)

    assert (
        "knowledge/notes/guide.md" in advisory,
        "knowledge/notes/alpha.md" in advisory,
        impact["stale_pages"],
    ) == (True, True, [])


def test_the_note_scan_is_not_paid_when_its_guesses_are_refused(tmp_path, monkeypatch):
    import impact_analysis

    scans: list[object] = []
    monkeypatch.setattr(
        impact_analysis, "find_stale_wiki_pages", lambda *args, **options: scans.append(args) or []
    )

    impact_analysis.analyze_impact(
        root=_changed_repository(tmp_path), graph=_Graph(), textual_fallback=False
    )

    assert scans == []


def test_session_start_gives_the_analysis_its_own_budget_and_skips_the_scan(monkeypatch):
    import impact_analysis
    import session_start_context

    calls: list[dict] = []

    def _recorded(**options):
        calls.append(options)
        return {"affected": {}, "summary": ""}

    monkeypatch.setattr(impact_analysis, "analyze_impact", _recorded)
    before = time.monotonic()

    block = session_start_context._impact_block()

    after = time.monotonic()
    budget = session_start_context.IMPACT_BUDGET_SECONDS
    assert (block, calls[0]["textual_fallback"]) == ("", False)
    assert before + budget <= calls[0]["deadline"] <= after + budget
