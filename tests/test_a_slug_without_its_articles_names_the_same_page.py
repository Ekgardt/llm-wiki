"""A drafted slug that differs from an existing page only by its articles updates that page.

The C-6 measurement found a draft proposing `the-x-y` beside an existing `x-y`; the
snapshot matched slugs exactly, so it would have created a near-duplicate. See
`docs/research/2026-09-27-a-slug-without-its-articles-names-the-same-page.md`.
"""

from __future__ import annotations

from llm_wiki import compile_pipeline_operations
from llm_wiki.compile_pipeline_operations import (
    with_snapshot_actions,
)
from llm_wiki.compile_pipeline_sources import (
    CompileInputs,
    TargetSnapshot,
)


def _page(slug: str, status: str = "active") -> TargetSnapshot:
    content = f"---\ntype: pattern\nstatus: {status}\n---\n# {slug}\n".encode()
    return TargetSnapshot(f"knowledge/notes/{slug}.md", content, "0" * 64)


def _inputs(*pages: TargetSnapshot) -> CompileInputs:
    return CompileInputs(dailies=(), sources=(), targets=pages)


def _create(slug: str) -> dict[str, object]:
    return {"action": "create", "slug": slug}


def _decided(operations: list[dict[str, object]], *pages: TargetSnapshot) -> list[tuple[object, object]]:
    kept = with_snapshot_actions(operations, _inputs(*pages))
    return [(operation["slug"], operation["action"]) for operation in kept]


def test_a_slug_with_an_extra_article_updates_the_existing_page() -> None:
    assert _decided([_create("the-exact-byte-pattern")], _page("exact-byte-pattern")) == [
        ("exact-byte-pattern", "update")
    ]


def test_a_slug_missing_an_article_updates_the_existing_page() -> None:
    assert _decided([_create("changelog-is-not-preamble")], _page("the-changelog-is-not-preamble")) == [
        ("the-changelog-is-not-preamble", "update")
    ]


def test_a_new_page_stays_a_create() -> None:
    assert _decided([_create("a-different-pattern")], _page("exact-byte-pattern")) == [
        ("a-different-pattern", "create")
    ]


def test_two_existing_pages_under_one_key_are_not_guessed(capsys) -> None:
    pages = (_page("exact-byte-pattern"), _page("the-exact-byte-pattern"))
    assert _decided([_create("an-exact-byte-pattern")], *pages) == [("an-exact-byte-pattern", "create")]
    assert "names 2 existing pages" in capsys.readouterr().err


def test_a_rename_never_collides_with_another_operation_of_the_plan() -> None:
    plan = [_create("the-exact-byte-pattern"), {"action": "update", "slug": "exact-byte-pattern"}]
    assert _decided(plan, _page("exact-byte-pattern")) == [
        ("the-exact-byte-pattern", "create"),
        ("exact-byte-pattern", "update"),
    ]


def test_the_page_it_names_is_still_refused_when_retired() -> None:
    assert _decided([_create("the-exact-byte-pattern")], _page("exact-byte-pattern", "superseded")) == []


def test_a_slug_of_only_articles_keys_to_itself() -> None:
    assert compile_pipeline_operations.slug_key("the") == "the"
    assert compile_pipeline_operations.slug_key("a-the-an") == "a-the-an"


def test_a_plural_slug_updates_the_singular_page() -> None:
    """The live vault held `accuracy-denominator(s)-answers-vs-questions` twice (2026-09-27)."""
    assert _decided(
        [_create("accuracy-denominators-answers-vs-questions")],
        _page("accuracy-denominator-answers-vs-questions"),
    ) == [("accuracy-denominator-answers-vs-questions", "update")]


def test_a_word_that_only_ends_in_s_is_not_a_plural() -> None:
    assert compile_pipeline_operations.slug_key("class-status-analysis") == "class-status-analysis"


def test_a_retired_duplicate_leaves_its_live_sibling_the_only_match() -> None:
    pages = (
        _page("accuracy-denominators-answers-vs-questions"),
        _page("accuracy-denominator-answers-vs-questions", status="superseded"),
    )
    assert _decided([_create("accuracy-denominator-answers-vs-questions")], *pages) == [
        ("accuracy-denominators-answers-vs-questions", "update")
    ]
