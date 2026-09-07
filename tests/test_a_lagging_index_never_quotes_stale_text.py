"""An index may lag. What it hands the model may not.

The review that prompted this made the distinction sharply: verifying a citation
protects the truthfulness of what was found, not the completeness of the search.
A document written after the snapshot is simply absent, and no check finds what
was never captured — that is the price of a lagging index and it is the one
Elasticsearch and every other near-real-time engine also pays.

The price that must *not* be paid is quoting a file the vault has moved on from.
Until 2026-09-05 the evidence handed to generation came straight out of the
snapshot, so a source edited since capture reached the model as current text and
was only caught afterwards, when its citation failed to resolve — too late to
stop it being read.

The sources about to be quoted are now re-read first. That is a handful of files
rather than the whole corpus, which is what makes it affordable per query.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pytest  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402
from query_memory import build_grounded_context  # noqa: E402


def _vault(tmp_path: Path, **pages: str) -> Path:
    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    for name, body in pages.items():
        (notes / f"{name}.md").write_text(body, encoding="utf-8")
    return vault


def _chunks_of(snapshot, name: str):
    return tuple(
        chunk
        for chunk in snapshot.chunks
        if chunk.source_path == f"knowledge/notes/{name}.md"
    )


def test_a_source_edited_after_capture_is_not_quoted(tmp_path: Path) -> None:
    vault = _vault(tmp_path, alpha="Alpha is enabled and has been for a while.")
    snapshot = collect_corpus(vault)
    chunks = _chunks_of(snapshot, "alpha")
    (vault / "knowledge" / "notes" / "alpha.md").write_text(
        "Alpha was disabled this morning.", encoding="utf-8"
    )

    context = build_grounded_context(snapshot, chunks, vault=vault, profile="BASE")

    assert context.evidence == ()
    assert context.stale_sources == ("knowledge/notes/alpha.md",)


def test_a_source_deleted_after_capture_is_not_quoted(tmp_path: Path) -> None:
    vault = _vault(tmp_path, alpha="Alpha is enabled.")
    snapshot = collect_corpus(vault)
    chunks = _chunks_of(snapshot, "alpha")
    (vault / "knowledge" / "notes" / "alpha.md").unlink()

    context = build_grounded_context(snapshot, chunks, vault=vault, profile="BASE")

    assert context.evidence == ()
    assert context.stale_sources == ("knowledge/notes/alpha.md",)


def test_an_untouched_source_beside_a_changed_one_still_answers(tmp_path: Path) -> None:
    """One moving file must not silence the rest — that is the old defect twice."""
    vault = _vault(
        tmp_path,
        alpha="Alpha is enabled.",
        beta="Beta runs every night at midnight.",
    )
    snapshot = collect_corpus(vault)
    chunks = _chunks_of(snapshot, "alpha") + _chunks_of(snapshot, "beta")
    (vault / "knowledge" / "notes" / "alpha.md").write_text("Alpha is gone.", encoding="utf-8")

    context = build_grounded_context(snapshot, chunks, vault=vault, profile="BASE")

    quoted = {item.relative_path for item in context.evidence}
    assert quoted == {"knowledge/notes/beta.md"}
    assert context.stale_sources == ("knowledge/notes/alpha.md",)


def test_nothing_is_dropped_when_the_vault_has_not_moved(tmp_path: Path) -> None:
    vault = _vault(tmp_path, alpha="Alpha is enabled.")
    snapshot = collect_corpus(vault)

    context = build_grounded_context(
        snapshot, _chunks_of(snapshot, "alpha"), vault=vault, profile="BASE"
    )

    assert context.stale_sources == ()
    assert [item.relative_path for item in context.evidence] == ["knowledge/notes/alpha.md"]


def test_a_snapshot_can_be_taken_while_the_vault_is_written(tmp_path: Path) -> None:
    """The whole point of a lagging index: capture succeeds under a live writer.

    Before 2026-09-05 a capture that lost this race gave up, and with it the
    nightly build — every night from 2026-08-30, leaving nothing active and
    retrieval on its lexical leg alone.
    """
    vault = _vault(tmp_path, alpha="Alpha is enabled.")
    daily = vault / "knowledge" / "daily"
    daily.mkdir()
    log = daily / "2026-09-05.md"
    log.write_text("# 2026-09-05\n\nstarted\n", encoding="utf-8")
    writes = {"n": 0}

    import corpus_snapshot

    original = corpus_snapshot._capture

    def writing_capture(root, policy, deadline, cancelled):
        writes["n"] += 1
        if writes["n"] <= 2:
            with log.open("a", encoding="utf-8") as stream:
                stream.write(f"line {writes['n']}\n")
            raise corpus_snapshot.CorpusChanged("corpus source changed during collection")
        return original(root, policy, deadline, cancelled)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(corpus_snapshot, "_capture", writing_capture)
        snapshot = corpus_snapshot.collect_corpus(vault)

    assert writes["n"] == 3
    assert snapshot.sources
