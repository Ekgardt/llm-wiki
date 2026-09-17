"""A trimmed guard-rails block says what it trimmed, and its file is denied by `.gitignore`.

Research: `docs/research/2026-09-17-the-guard-rails-say-how-many-rules-they-left-out.md`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _pattern_pages(count: int) -> dict[str, bytes]:
    page = "---\ntype: pattern\n---\n# Rule {n}\n\nOne-sentence summary: Never do thing number {n} again.\n"
    return {f"knowledge/notes/rule-{n:02d}.md": page.format(n=n).encode() for n in range(count)}


def test_a_trimmed_block_names_both_what_the_type_and_the_ceiling_left_out():
    import build_guardrails

    block = build_guardrails.build_guardrails(source_contents=_pattern_pages(20))

    assert ("**PATTERN** (5 of 15 shown):" in block, block.splitlines()[-1]) == (
        True,
        "(5 more rule(s) past the ceiling of 15 are not shown)",
    )


def test_an_untrimmed_block_keeps_its_plain_count():
    import build_guardrails

    block = build_guardrails.build_guardrails(source_contents=_pattern_pages(3))

    assert ("**PATTERN** (3):" in block, "not shown" in block) == (True, False)


def _rule_page(slug: str, *, authority: str, stated_at: str, summary: str) -> tuple:
    page = (
        "---\ntype: pattern\n"
        f"source_authority: {authority}\ntimestamp: {stated_at}\n"
        f"---\n# {slug}\n\nOne-sentence summary: Never {summary}.\n"
    )
    return (f"knowledge/notes/{slug}.md", page.encode())


def _shown_summaries(block: str) -> list[str]:
    return [line[2:] for line in block.splitlines() if line.startswith("- ")]


def test_the_slots_go_to_the_stated_rule_before_the_inferred_one():
    """Rule 13's hierarchy decides which rule gets the slot, not the alphabet."""
    import build_guardrails

    sources = dict(
        [
            _rule_page("aaa", authority="inferred", stated_at="2026-09-01", summary="do A"),
            _rule_page("zzz", authority="user", stated_at="2026-08-01", summary="do Z"),
        ]
    )

    block = build_guardrails.build_guardrails(source_contents=sources, max_rules=1)

    assert _shown_summaries(block) == ["Never do Z."]


def test_between_equal_rules_the_newer_one_is_shown():
    import build_guardrails

    sources = dict(
        [
            _rule_page("aaa", authority="user", stated_at="2026-01-01", summary="do A"),
            _rule_page("zzz", authority="user", stated_at="2026-09-16", summary="do Z"),
        ]
    )

    block = build_guardrails.build_guardrails(source_contents=sources, max_rules=1)

    assert _shown_summaries(block) == ["Never do Z."]


def test_a_rule_that_declares_no_time_comes_after_one_that_does():
    import build_guardrails

    dated = _rule_page("zzz", authority="user", stated_at="2026-01-01", summary="do Z")
    undated = (
        "knowledge/notes/aaa.md",
        b"---\ntype: pattern\nsource_authority: user\n---\n# aaa\n\n"
        b"One-sentence summary: Never do A.\n",
    )

    block = build_guardrails.build_guardrails(
        source_contents=dict([dated, undated]), max_rules=1
    )

    assert _shown_summaries(block) == ["Never do Z."]


def test_the_written_guard_rails_file_is_denied_by_gitignore():
    checked = subprocess.run(
        ["git", "check-ignore", "-q", "knowledge/guardrails.md"], cwd=REPOSITORY, check=False
    )

    assert checked.returncode == 0
