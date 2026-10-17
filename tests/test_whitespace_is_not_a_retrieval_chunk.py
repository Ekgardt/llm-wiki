"""A paragraph cut must not publish blank evidence that its validator rejects."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import corpus_snapshot as corpus
import pytest
import search_memory as search


@pytest.mark.parametrize("tail", ["\n\n", " \t\n", "\u2003\u2003\n"])
def test_a_blank_tail_does_not_break_the_real_fts_artifact(tmp_path, tail):
    page = tmp_path / "knowledge/notes/evidence.md"
    page.parent.mkdir(parents=True)
    content = ("# Heading\n\n" + "x" * corpus.MAX_SPAN_BYTES + tail).encode()
    page.write_bytes(content)
    snapshot = corpus.collect_corpus(tmp_path)
    assert snapshot.sources[0].content == content
    directory = tmp_path / "generation"
    directory.mkdir()
    search.build_generation_fts(snapshot, directory)
    manifest = {
        "collector_version": snapshot.collector_version,
        "extractor_version": snapshot.extractor_version,
        "source_manifest_sha256": snapshot.corpus_sha256,
    }
    sources = {
        s.record.logical_id: {
            "relative_path": s.record.relative_path,
            "sha256": s.record.sha256,
            "content": s.content,
        }
        for s in snapshot.sources
    }
    with closing(sqlite3.connect(directory / search.GENERATION_FTS_ARTIFACT)) as connection:
        assert search._valid_generation_fts(connection, manifest, authoritative_sources=sources)
    assert all(chunk.text.strip() for chunk in snapshot.chunks)
    assert "".join(chunk.text for chunk in snapshot.chunks).strip() == content.decode().strip()
    assert all(content[c.byte_start : c.byte_end].decode() == c.text for c in snapshot.chunks)
