"""The tracked index must not name a page this repository does not publish.

The vault and the public source are one directory. `knowledge/index.md` is
tracked and the runtime rewrites it, while `knowledge/notes/` is denied by
default with an explicit allowlist. So the generator has to know which pages
are public; otherwise every rebuild writes private page titles into a public
file, and only the structure test at commit time stands between that and a
push.

The answer is read out of `.gitignore` rather than asked of git, because the
index rebuild is an automatic writer and no automatic writer here runs git.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

PAGE = """---
type: concept
title: "{title}"
---
# {title}

One-sentence summary: {title} exists.
"""

DENY_WITH_ALLOWLIST = "knowledge/notes/*\n!knowledge/notes/published-page.md\n"


def _write_page(notes: Path, slug: str) -> None:
    (notes / f"{slug}.md").write_text(PAGE.format(title=slug), encoding="utf-8")


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    _write_page(notes, "published-page")
    _write_page(notes, "private-page")
    (tmp_path / ".gitignore").write_text(DENY_WITH_ALLOWLIST, encoding="utf-8")
    return tmp_path


def test_a_private_page_never_reaches_the_index(vault: Path) -> None:
    import rebuild_memory_index

    rendered = rebuild_memory_index.build_index_bytes(vault).decode("utf-8")

    assert "published-page" in rendered
    assert "private-page" not in rendered


def test_a_private_page_supplied_as_a_pending_image_is_also_dropped(
    vault: Path,
) -> None:
    """Transaction after-images take the same path as pages read from disk."""
    import rebuild_memory_index

    pending = {
        "knowledge/notes/private-page.md": PAGE.format(
            title="private-page"
        ).encode("utf-8")
    }
    rendered = rebuild_memory_index.build_index_bytes(
        vault, pending, base={}
    ).decode("utf-8")

    assert "private-page" not in rendered


def test_a_vault_that_publishes_nothing_is_left_alone(tmp_path: Path) -> None:
    """A private vault has no allowlist, and filtering it would empty its index."""
    import rebuild_memory_index

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    _write_page(notes, "some-page")

    rendered = rebuild_memory_index.build_index_bytes(tmp_path).decode("utf-8")

    assert "some-page" in rendered


def test_the_repositorys_own_allowlist_is_read(tmp_path: Path) -> None:
    """Guards the coupling: the allowlist form in .gitignore is the contract."""
    import rebuild_memory_index

    repository = Path(__file__).resolve().parent.parent
    published = rebuild_memory_index._published_notes(repository)

    assert published is not None
    assert "knowledge/notes/single-directory-vault-decision.md" in published
