"""A day of sessions becomes durable knowledge, and only what is quotable does.

Keeping the sessions was the first half (MEM-01). Nothing read them, so nothing
became a page. The idle window is where the reading happens, and every promoted
item has to quote the record it came from.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import episode_consolidation as consolidation  # noqa: E402

RECORD = """---
type: raw-source
session: abc123
---

# Session abc123

**user:** why systemd and not cron?

**assistant:** systemd user timers survive a reboot and cron does not report failures
"""


def _answer(items) -> str:
    return json.dumps(items, ensure_ascii=False)


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    directory = tmp_path / "knowledge/raw/sessions/2026-08-23"
    directory.mkdir(parents=True)
    (directory / "abc123.md").write_text(RECORD, encoding="utf-8")
    return tmp_path


def test_a_quoted_item_survives(vault: Path) -> None:
    lessons = consolidation.grounded_lessons(
        _answer(
            [
                {
                    "kind": "decision",
                    "text": "systemd user timers were chosen over cron",
                    "quote": "systemd user timers survive a reboot",
                    "session": "abc123",
                }
            ]
        ),
        consolidation.session_records(vault, "2026-08-23"),
    )

    assert [item.kind for item in lessons] == ["decision"]
    assert lessons[0].session == "abc123"


def test_an_invented_quote_is_dropped(vault: Path) -> None:
    """The validation the survey asks for before promotion, done literally."""
    lessons = consolidation.grounded_lessons(
        _answer(
            [
                {
                    "kind": "decision",
                    "text": "we switched to launchd",
                    "quote": "launchd was chosen because it is faster",
                    "session": "abc123",
                }
            ]
        ),
        consolidation.session_records(vault, "2026-08-23"),
    )

    assert lessons == []


def test_an_unknown_kind_is_dropped(vault: Path) -> None:
    lessons = consolidation.grounded_lessons(
        _answer(
            [
                {
                    "kind": "rumour",
                    "text": "something",
                    "quote": "systemd user timers survive a reboot",
                    "session": "abc123",
                }
            ]
        ),
        consolidation.session_records(vault, "2026-08-23"),
    )

    assert lessons == []


def test_a_non_json_answer_is_refused(vault: Path) -> None:
    with pytest.raises(ValueError, match="JSON array"):
        consolidation.grounded_lessons("I could not do that", [])


def test_the_block_is_one_entry_with_its_quotes(vault: Path) -> None:
    lesson = consolidation.Lesson(
        "decision", "systemd over cron", "systemd user timers survive a reboot", "abc123"
    )

    block = consolidation.render_block(
        "2026-08-23", [lesson], datetime(2026, 8, 24, 3, 15, 0)
    )

    assert block.startswith("- `[03:15:00] episodes | 2026-08-23`")
    assert "> systemd user timers survive a reboot" in block
    assert block.count("- `[") == 1, "the compile pipeline binds evidence inside one entry"


def test_a_day_with_records_is_consolidated_and_recorded(vault: Path, monkeypatch) -> None:
    written = {}
    recorded = {}
    monkeypatch.setattr(
        consolidation,
        "_write_block",
        lambda day, lessons, moment: written.setdefault("day", day) or Path("daily.md"),
    )
    monkeypatch.setattr(
        consolidation,
        "_record_consolidation",
        lambda day, count, records: recorded.update(day=day, count=count, records=records),
    )

    outcome = consolidation.consolidate_day(
        vault,
        "2026-08-23",
        call=lambda _prompt: _answer(
            [
                {
                    "kind": "lesson",
                    "text": "cron does not report failures",
                    "quote": "cron does not report failures",
                    "session": "abc123",
                }
            ]
        ),
        state={},
    )

    assert outcome["status"] == "written"
    assert outcome["items"] == 1
    assert recorded == {"day": "2026-08-23", "count": 1, "records": 1}


def test_a_day_already_consolidated_is_left_alone(vault: Path) -> None:
    outcome = consolidation.consolidate_day(
        vault,
        "2026-08-23",
        call=lambda _prompt: pytest.fail("the provider must not be called again"),
        state={"consolidated_session_days": {"2026-08-23": {"items": 0}}},
    )

    assert outcome == {"status": "skipped", "reason": "already_consolidated", "items": 0}


def test_a_day_without_records_costs_nothing(tmp_path: Path) -> None:
    outcome = consolidation.consolidate_day(
        tmp_path,
        "2026-08-23",
        call=lambda _prompt: pytest.fail("the provider must not be called"),
        state={},
    )

    assert outcome == {"status": "skipped", "reason": "no_records", "items": 0}


def test_a_day_with_nothing_durable_is_marked_done(vault: Path, monkeypatch) -> None:
    """An empty day must not be re-read every night."""
    recorded = {}
    monkeypatch.setattr(
        consolidation,
        "_record_consolidation",
        lambda day, count, records: recorded.update(day=day, count=count),
    )

    outcome = consolidation.consolidate_day(
        vault, "2026-08-23", call=lambda _prompt: "[]", state={}
    )

    assert outcome["status"] == "empty"
    assert recorded == {"day": "2026-08-23", "count": 0}


def test_the_prompt_carries_the_records(vault: Path) -> None:
    prompt = consolidation.build_prompt(
        "2026-08-23", consolidation.session_records(vault, "2026-08-23")
    )

    assert "=== session abc123 ===" in prompt
    assert "systemd user timers survive a reboot" in prompt


def _rule(text: str, trigger: str) -> str:
    return _answer(
        [
            {
                "kind": "rule",
                "text": text,
                "trigger": trigger,
                "quote": "systemd user timers survive a reboot",
                "session": "abc123",
            }
        ]
    )


def test_a_rule_needs_a_situation_and_an_instruction(vault: Path) -> None:
    """Procedural memory is read before acting, so it must say when and what."""
    lessons = consolidation.grounded_lessons(
        _rule("always prefer a user timer over cron", "scheduling maintenance"),
        consolidation.session_records(vault, "2026-08-23"),
    )

    assert [item.kind for item in lessons] == ["rule"]
    assert lessons[0].trigger == "scheduling maintenance"


def test_a_rule_without_a_trigger_is_not_a_rule(vault: Path) -> None:
    lessons = consolidation.grounded_lessons(
        _rule("always prefer a user timer over cron", ""),
        consolidation.session_records(vault, "2026-08-23"),
    )

    assert lessons == []


def test_a_rule_that_reads_like_a_note_is_not_a_rule(vault: Path) -> None:
    """`build_guardrails` recognises rules by these words; without one it is a lesson."""
    lessons = consolidation.grounded_lessons(
        _rule("user timers are nicer than cron", "scheduling maintenance"),
        consolidation.session_records(vault, "2026-08-23"),
    )

    assert lessons == []


def test_a_rule_states_its_situation_first(vault: Path) -> None:
    lesson = consolidation.Lesson(
        "rule",
        "always prefer a user timer over cron",
        "systemd user timers survive a reboot",
        "abc123",
        "scheduling maintenance",
    )

    block = consolidation.render_block(
        "2026-08-23", [lesson], datetime(2026, 8, 24, 3, 15, 0)
    )

    assert "**Rule** — When scheduling maintenance: always prefer a user timer" in block


def test_a_rule_summary_is_shaped_for_the_session_start_surface(vault: Path) -> None:
    """The loop only closes if `build_guardrails` would pick the page up."""
    import re

    lesson = consolidation.Lesson(
        "rule",
        "never resolve a merge conflict automatically",
        "systemd user timers survive a reboot",
        "abc123",
        "updating the checkout",
    )
    block = consolidation.render_block("2026-08-23", [lesson], datetime.now())

    assert re.search(
        r"\b(do not|don'?t|always|never|must|should)\b", block, re.IGNORECASE
    )


def test_a_busy_day_is_read_in_batches_not_truncated(tmp_path, monkeypatch):
    """Every record of a day is read; a day used to stop at the twelfth.

    The imported history has a day with 171 sessions. Truncating silently marked
    the other 159 consolidated without ever reading them.
    """
    import episode_consolidation

    vault = tmp_path / "vault"
    day = "2026-08-23"
    directory = vault / "knowledge/raw/sessions" / day
    directory.mkdir(parents=True)
    for index in range(episode_consolidation.MAX_RECORDS * 2 + 1):
        (directory / f"s{index:03d}.md").write_text(
            f"---\ntype: raw-source\n---\n# Session s{index:03d}\n\nuser: line {index}\n",
            encoding="utf-8",
        )

    seen: list[int] = []

    def call(prompt: str) -> str:
        seen.append(prompt.count("=== session "))
        return "[]"

    outcome = episode_consolidation.consolidate_day(vault, day, call=call, state={})

    assert outcome["batches"] == 3
    assert seen == [episode_consolidation.MAX_RECORDS, episode_consolidation.MAX_RECORDS, 1]


def test_pending_days_are_the_ones_never_consolidated(tmp_path):
    import episode_consolidation

    vault = tmp_path / "vault"
    for day in ("2026-08-06", "2026-08-08", "2026-08-16"):
        directory = vault / "knowledge/raw/sessions" / day
        directory.mkdir(parents=True)
        (directory / "s.md").write_text("---\ntype: raw-source\n---\n# S\n", encoding="utf-8")

    state = {"consolidated_session_days": {"2026-08-08": {"items": 0}}}

    assert episode_consolidation.pending_days(vault, state) == [
        "2026-08-06",
        "2026-08-16",
    ]


def test_two_batches_of_one_day_get_distinct_timestamps(tmp_path, monkeypatch):
    """A timestamp locates an entry, so two entries must not share one.

    Twelve consolidation entries landed in the same second on 2026-08-24, and the
    compile refused every piece of evidence that named it: the timestamp was
    ambiguous, so no plan for that day could validate.
    """
    import episode_consolidation

    vault = tmp_path / "vault"
    day = "2026-08-23"
    directory = vault / "knowledge/raw/sessions" / day
    directory.mkdir(parents=True)
    for index in range(episode_consolidation.MAX_RECORDS + 1):
        (directory / f"s{index:03d}.md").write_text(
            f"---\ntype: raw-source\n---\n# Session\n\nuser: line {index}\n",
            encoding="utf-8",
        )

    moments: list = []
    monkeypatch.setattr(
        episode_consolidation,
        "_write_block",
        lambda day, lessons, moment: moments.append(moment) or Path("daily.md"),
    )
    monkeypatch.setattr(episode_consolidation, "_record_consolidation", lambda *a: None)
    monkeypatch.setattr(
        episode_consolidation,
        "grounded_lessons",
        lambda raw, paths: [
            episode_consolidation.Lesson("lesson", "text", "quote", "session")
        ],
    )

    episode_consolidation.consolidate_day(
        vault, day, call=lambda prompt: "[]", state={},
        moment=datetime(2026, 8, 24, 13, 25, 41),
    )

    assert len(moments) == 2
    assert moments[0] != moments[1]
