"""With no active generation a natural question still finds the pages that answer it.

See docs/research/2026-09-25-a-question-finds-pages-without-a-generation.md.
"""

from __future__ import annotations

from pathlib import Path

import generation_search


def _page(root: Path, name: str, body: str) -> Path:
    path = root / "knowledge" / "notes" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: concept\n---\n# {name}\n\n{body}\n", encoding="utf-8")
    return path


def test_a_question_in_russian_finds_the_page_that_shares_its_words(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    pages = [
        _page(tmp_path, "nightly-scheduler", "\u041c\u044b \u0432\u044b\u0431\u0440\u0430\u043b\u0438 systemd \u0442\u0430\u0439\u043c\u0435\u0440 \u0432\u043c\u0435\u0441\u0442\u043e cron: \u043e\u043d \u043f\u0435\u0440\u0435\u0436\u0438\u0432\u0430\u0435\u0442 \u043f\u0435\u0440\u0435\u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0443."),
        _page(tmp_path, "unrelated", "\u0417\u0430\u043c\u0435\u0442\u043a\u0430 \u043e \u0434\u0440\u0443\u0433\u043e\u043c."),
    ]

    hits = generation_search.direct_markdown_hits(
        "\u043f\u043e\u0447\u0435\u043c\u0443 systemd \u0442\u0430\u0439\u043c\u0435\u0440, \u0430 \u043d\u0435 cron", pages, limit=5, project=None, since=None,
        as_of=None, deadline=None, cancelled=None,
    )

    assert [Path(hit["path"]).stem for hit in hits] == ["nightly-scheduler"]


def test_a_page_sharing_more_words_ranks_first(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    pages = [
        _page(tmp_path, "one-word", "cron only"),
        _page(tmp_path, "three-words", "systemd timer instead of cron"),
    ]

    hits = generation_search.direct_markdown_hits(
        "why a systemd timer and not cron", pages, limit=5, project=None, since=None,
        as_of=None, deadline=None, cancelled=None,
    )

    assert [Path(hit["path"]).stem for hit in hits] == ["three-words", "one-word"]
