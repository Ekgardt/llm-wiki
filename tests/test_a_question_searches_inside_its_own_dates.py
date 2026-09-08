"""A question that names a time runs one more search inside that time.

LongMemEval's time-aware expansion is worth +6.8 to +11.3 points on temporal
questions. A daily entry now carries the date of its file name as
`valid_from`, so the index's since/as-of window reaches it, and a question
whose expressions resolve to dates searches inside [earliest - 3, latest + 3]
and merges what it finds with the first candidates.
See `docs/research/2026-09-08-the-calendar-does-the-arithmetic.md`.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from corpus_snapshot import collect_corpus  # noqa: E402
from query_memory import grounded_qa  # noqa: E402
from temporal_anchor import window  # noqa: E402


def test_a_daily_entry_is_dated_by_its_file_name(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    (root / "knowledge" / "daily").mkdir(parents=True)
    (root / "knowledge" / "daily" / "2023-05-27.md").write_text("# 2023-05-27\n\nI beat the game.\n")

    snapshot = collect_corpus(root, code_roots=(), daily_paths=["knowledge/daily/2023-05-27.md"])

    assert {chunk.valid_from for chunk in snapshot.chunks} == {"2023-05-27"}


def test_the_window_covers_the_resolved_days_three_out_each_side() -> None:
    asked = date(2023, 5, 30)

    assert window("Which book did I finish a week ago?", asked) == ("2023-05-20", "2023-05-26")
    assert window("How many bikes do I own?", asked) is None


def _refusal(prompt: str) -> str:
    return json.dumps(
        {
            "schema_version": "grounded-answer/v1",
            "status": "insufficient_evidence",
            "claims": [],
            "citations": [],
            "reason": "nothing",
        }
    )


def test_a_dated_question_searches_inside_its_days_and_merges(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "daily").mkdir(parents=True)
    (root / "knowledge" / "notes" / "a.md").write_text("---\ntype: concept\n---\n\n# A\n\nAlpha.\n")
    snapshot = collect_corpus(root)
    calls: list[dict] = []

    def search(query: str, limit: int, **window) -> tuple:
        calls.append(window)
        return tuple(snapshot.chunks)

    grounded_qa(
        "Which book did I finish a week ago?\n(Current date: 2023/05/30 (Tue) 10:00)",
        vault=root,
        snapshot=snapshot,
        retrieve=lambda limit: (),
        search=search,
        generator=lambda prompt, system_prompt, max_tokens: _refusal(prompt),
        profile="BASE",
    )

    assert calls[0] == {"since": "2023-05-20", "as_of": "2023-05-26"}


def test_a_question_without_a_date_runs_no_dated_leg(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "daily").mkdir(parents=True)
    (root / "knowledge" / "notes" / "a.md").write_text("---\ntype: concept\n---\n\n# A\n\nAlpha.\n")
    snapshot = collect_corpus(root)
    calls: list[dict] = []

    def search(query: str, limit: int, **window) -> tuple:
        calls.append(window)
        return ()

    grounded_qa(
        "How many bikes do I own?",
        vault=root,
        snapshot=snapshot,
        retrieve=lambda limit: tuple(snapshot.chunks),
        search=search,
        generator=lambda prompt, system_prompt, max_tokens: _refusal(prompt),
        profile="BASE",
    )

    assert not any(call for call in calls if "since" in call)
