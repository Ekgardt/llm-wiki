"""A batch is offered the pages its days are about first, not the early ones by path.

See docs/research/2026-09-25-the-compile-sees-the-pages-its-day-is-about.md.
"""

from __future__ import annotations

from compile_memory import SourceSnapshot, _ContextRanking


def _page(name: str, text: str) -> SourceSnapshot:
    return SourceSnapshot(f"knowledge/notes/{name}.md", text.encode(), "0" * 64)


PAGES = [
    _page("aardvark-notes", "General remarks about release notes and packaging."),
    _page("alpha-setup", "Installation steps for the scheduler timers."),
    _page("zz-cold-search", "The first search call loads the encoder model; cold search latency."),
]


def test_the_page_about_the_day_comes_first() -> None:
    day = b"Measured the cold search again: the encoder model loads in 2.3 s."

    ordered = _ContextRanking(PAGES).ordered(day)

    assert ordered[0].logical_path == "knowledge/notes/zz-cold-search.md"


def test_with_nothing_in_common_the_order_is_by_path() -> None:
    ordered = _ContextRanking(PAGES).ordered(b"")

    assert [item.logical_path for item in ordered] == sorted(item.logical_path for item in PAGES)
