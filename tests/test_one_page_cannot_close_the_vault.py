"""One page with unreadable metadata costs its metadata, never the corpus.

Each frontmatter below used to make `collect_corpus` raise and return nothing, for
the nightly build, MCP and every answer alike. Research:
`docs/research/2026-09-14-one-page-cannot-close-the-vault.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from tests.test_corpus_snapshot import write  # noqa: E402


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    return root


BROKEN = {
    "colon": "---\ntitle: Fix: the thing\ntype: concept\n---\n# Body\nKept words.\n",
    "empty": "---\n---\n# Body\nKept words.\n",
    "unclosed": "---\ntype: concept\n# Body\nKept words.\n",
    "list-type": "---\ntype: [a, b]\nproject: [x]\n---\n# Body\nKept words.\n",
    "validity": "---\ntype: concept\nvalidity: always\n---\n# Body\nKept words.\n",
    "control": "---\ntitle: bell \x07 here\n---\n# Body\nKept words.\n",
}


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_the_rest_of_the_vault_is_still_read(vault, name):
    from corpus_snapshot import collect_corpus

    write(vault / "knowledge/notes/good.md", "---\ntype: concept\n---\n# Good\nFine.\n")
    write(vault / f"knowledge/notes/{name}.md", BROKEN[name])

    paths = {source.record.relative_path for source in collect_corpus(vault).sources}

    assert {"knowledge/notes/good.md", f"knowledge/notes/{name}.md"} <= paths


def test_a_page_that_is_not_text_costs_only_itself(vault):
    from corpus_snapshot import collect_corpus

    write(vault / "knowledge/notes/good.md", "# Good\nFine.\n")
    (vault / "knowledge/notes/latin.md").write_bytes(b"# Caf\xe9\n")

    paths = {source.record.relative_path for source in collect_corpus(vault).sources}

    assert paths == {"knowledge/notes/good.md"}


def test_an_unreadable_validity_bound_does_not_stop_an_as_of_read(vault):
    from corpus_snapshot import collect_corpus

    write(vault / "knowledge/notes/when.md", "---\ntype: concept\nvalid_from: someday\n---\n# When\n")

    assert len(collect_corpus(vault, as_of="2026-01-01").sources) == 1


def test_lint_names_every_page_and_what_was_dropped():
    from corpus_snapshot import frontmatter_problems

    named = {name: frontmatter_problems(text.encode()) for name, text in BROKEN.items()}

    assert [name for name, problems in sorted(named.items()) if not problems] == ["empty"]
    assert named["list-type"] == ["`type` is not a single value", "`project` is not a single value"]


def test_compile_cannot_write_a_character_yaml_refuses():
    import yaml
    from compile_memory import _escape_yaml

    title = _escape_yaml("bell \x07 escape \x1b del \x7f end")

    assert yaml.safe_load(f'title: "{title}"') == {"title": "bell  escape  del  end"}
