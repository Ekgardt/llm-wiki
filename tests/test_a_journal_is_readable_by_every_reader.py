"""A page the project journal accepts is one every reader of it accepts.

Four bounds met the same file, `knowledge/projects/<slug>/journal.md`, and
two of them were smaller than what the journal itself allows. A 4.2 MB
journal was refused by the claim index for three days after the claim
tree's cap had been raised (2026-09-10). See
`docs/research/2026-09-10-one-ceiling-for-every-reader-of-a-journal.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claim_tree_manifest  # noqa: E402
import claims  # noqa: E402
import lint_memory  # noqa: E402
import project_journal  # noqa: E402


def test_every_reader_of_a_journal_accepts_what_the_journal_may_be() -> None:
    ceiling = project_journal.MAX_JOURNAL_BYTES
    readers = {
        "claim tree": claim_tree_manifest.MAX_CLAIM_TREE_FILE_BYTES,
        "claim index": claims.MAX_CLAIM_PAGE_BYTES,
        "lint": lint_memory.MAX_LINT_PAGE_BYTES,
    }
    too_small = {name: cap for name, cap in readers.items() if cap < ceiling}
    assert too_small == {}, f"readers below the journal ceiling {ceiling}: {too_small}"


def test_the_claim_readers_share_one_file_set_and_it_has_no_journal(tmp_path: Path) -> None:
    """The journal is the event log the state is projected from, not a page
    that carries claims; three readers name the same two files from one place."""
    assert claim_tree_manifest.PROJECT_CLAIM_FILES == {"context.md", "state.md"}
    project = tmp_path / "knowledge" / "projects" / "demo"
    project.mkdir(parents=True)
    for name in ("context.md", "journal.md", "state.md", "other.md"):
        (project / name).write_text("---\ntype: project-state\n---\n# X\n", encoding="utf-8")
    projects = tmp_path / "knowledge" / "projects"
    assert [p.name for p in lint_memory._project_claim_pages(projects)] == ["context.md", "state.md"]
    assert sorted(p.name for p in claims._project_pages(projects)) == ["context.md", "state.md"]


def test_a_journal_past_every_cap_no_longer_stops_a_claim_rebuild(tmp_path: Path) -> None:
    """The live failure: a 4.2 MB journal refused the whole compile for three days."""
    vault = tmp_path / "vault"
    project = vault / "knowledge" / "projects" / "demo"
    (vault / "knowledge" / "notes").mkdir(parents=True)
    project.mkdir(parents=True)
    (project / "state.md").write_text("---\ntype: project-state\n---\n# S\n", encoding="utf-8")
    (project / "journal.md").write_bytes(b"#" + b"x" * (claims.MAX_CLAIM_PAGE_BYTES + 1))
    index = claims.ClaimIndex(tmp_path / "state", vault=vault)
    index.rebuild()
    assert index.path.is_file()


def test_every_reader_of_a_knowledge_page_shares_one_ceiling() -> None:
    """Audit M4: the family, not the pair — one ceiling declared in bounded_io."""
    import access_tracking
    import bounded_io
    import compile_memory
    import corpus_snapshot
    import rebuild_memory_index
    import repair_backlinks
    import search_memory

    ceilings = {
        "journal": project_journal.MAX_JOURNAL_BYTES,
        "claim_tree": claim_tree_manifest.MAX_CLAIM_TREE_FILE_BYTES,
        "guardrails": claim_tree_manifest.MAX_GUARDRAIL_SOURCE_FILE_BYTES,
        "claims": claims.MAX_CLAIM_PAGE_BYTES,
        "lint": lint_memory.MAX_LINT_PAGE_BYTES,
        "corpus": corpus_snapshot.MAX_CORPUS_FILE_BYTES,
        "search": search_memory.MAX_PAGE_BYTES,
        "after_image": compile_memory.MAX_AFTER_IMAGE_BYTES,
        "index": rebuild_memory_index.MAX_PAGE_BYTES,
        "backlinks": repair_backlinks.MAX_PAGE_BYTES,
        "access": access_tracking.MAX_ACCESS_PAGE_BYTES,
    }

    assert set(ceilings.values()) == {bounded_io.MAX_KNOWLEDGE_PAGE_BYTES}
    assert bounded_io.MAX_KNOWLEDGE_PAGE_BYTES == 8 * 1024 * 1024
