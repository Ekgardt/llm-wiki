"""A derived corpus cannot retain every expanded chunk object."""
import hashlib
import time
from dataclasses import FrozenInstanceError

import corpus_snapshot as corpus
import pytest


def _notes(vault, contents):
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    for index, content in enumerate(contents):
        (notes / f"page-{index}.md").write_bytes(content)
    return corpus.collect_corpus(vault)


def test_real_100001_chunks_keep_both_bounded_sources(tmp_path):
    snapshot = _notes(tmp_path, (b"# H\nx\n" * 50001, b"# H\nx\n" * 50000))
    assert len(snapshot.chunks) == 100001
    assert not isinstance(snapshot.chunks, tuple)
    assert sum(1 for _ in snapshot.chunks) == 100001
    assert snapshot.chunks[-1].source_path.endswith("page-1.md")


def test_captured_bytes_survive_source_deletion_and_keep_citations(tmp_path):
    snapshot = _notes(tmp_path, ("# Тема\nТочный cafe\u0301.\n## Вторая\nХвост.\n".encode(),))
    expected = tuple(snapshot.chunks)
    (tmp_path / expected[0].source_path).unlink()
    assert snapshot.chunks == expected
    for chunk in snapshot.chunks:
        raw = snapshot.sources[0].content[chunk.byte_start:chunk.byte_end]
        assert raw.decode() == chunk.text
        assert hashlib.sha256(raw).hexdigest() == chunk.span_sha256


def test_lazy_slices_indices_and_equality_preserve_sequence_contract(tmp_path):
    snapshot = _notes(tmp_path, (b"# One\nfirst\n## Two\nsecond\n", b"# Three\nlast\n"))
    values = tuple(snapshot.chunks)
    assert tuple(snapshot.chunks[::-1]) == values[::-1]
    assert snapshot.chunks[:2] == values[:2]
    assert not isinstance(snapshot.chunks[:], tuple)
    with pytest.raises(IndexError):
        snapshot.chunks[len(values)]
    with pytest.raises(TypeError):
        snapshot.chunks[0] = values[0]
    with pytest.raises(FrozenInstanceError):
        snapshot.chunks.plans = ()


def test_each_iteration_uses_its_own_deadline_not_old_collection_clock(tmp_path, monkeypatch):
    snapshot = _notes(tmp_path, (b"# One\nfirst\n",))
    now = time.monotonic()
    def later_clock():
        return now + 1000

    monkeypatch.setattr(corpus.time, "monotonic", later_clock)
    assert len(tuple(corpus.iter_snapshot_chunks(snapshot))) == len(snapshot.chunks)
    with pytest.raises(TimeoutError):
        tuple(corpus.iter_snapshot_chunks(snapshot, deadline=now))


def test_tuple_fixture_fallback_checks_deadline_and_cancellation(tmp_path):
    snapshot = _notes(tmp_path, (b"# One\nfirst\n",))
    from dataclasses import replace
    fixture = replace(snapshot, chunks=tuple(snapshot.chunks))
    assert tuple(corpus.iter_snapshot_chunks(fixture)) == fixture.chunks
    with pytest.raises(TimeoutError):
        tuple(corpus.iter_snapshot_chunks(fixture, deadline=time.monotonic() - 1))
    with pytest.raises(TimeoutError):
        tuple(corpus.iter_snapshot_chunks(fixture, cancelled=_cancelled))


def _cancelled():
    return True


def test_empty_sequence_and_empty_slice_still_refuse_expired_clock(tmp_path):
    snapshot = _notes(tmp_path, (b"# One\nfirst\n",))
    with pytest.raises(TimeoutError):
        tuple(snapshot.chunks[:0].iter_chunks(deadline=time.monotonic() - 1))
    empty = _notes(tmp_path / "empty", ())
    with pytest.raises(TimeoutError):
        tuple(corpus.iter_snapshot_chunks(empty, deadline=time.monotonic() - 1))


def test_streaming_cancellation_interrupts_actual_source_derivation(tmp_path):
    snapshot = _notes(tmp_path, (b"# H\nx\n" * 100,))
    calls = []

    def cancel_during_work():
        calls.append(True)
        return len(calls) > 6

    with pytest.raises(TimeoutError, match="cancelled"):
        tuple(corpus.iter_snapshot_chunks(snapshot, cancelled=cancel_during_work))
    assert len(calls) == 7


def test_rederived_count_mismatch_is_visible_refusal(tmp_path):
    from dataclasses import replace

    snapshot = _notes(tmp_path, (b"# H\nx\n",))
    plan = replace(snapshot.chunks.plans[0], count=2)
    forged = replace(snapshot.chunks, plans=(plan,))
    with pytest.raises(ValueError, match="chunk count changed"):
        tuple(forged)


def test_chunk_fields_are_pinned_when_public_source_descriptor_is_reclassified(tmp_path):
    snapshot = _notes(tmp_path, (b"# One\nfirst\n",))
    expected = tuple(snapshot.chunks)
    source = snapshot.sources[0]
    object.__setattr__(source.record, "language", "typescript")
    object.__setattr__(source.record, "sha256", "0" * 64)
    object.__setattr__(source.metadata, "type", "workflow")
    assert tuple(snapshot.chunks) == expected


def test_source_selection_is_a_lazy_ordered_view_of_captured_plans(tmp_path):
    snapshot = _notes(tmp_path, (b"# One\nfirst\n", b"# Two\nsecond\n"))
    expected = tuple(snapshot.chunks)[1:]
    selected = corpus.select_snapshot_chunks(snapshot, (expected[0].source_path,))
    assert isinstance(selected, corpus.CapturedChunks)
    assert len(selected) == 1
    assert tuple(selected) == expected
    assert selected.plans[0] is snapshot.chunks.plans[1]
    assert len(corpus.select_snapshot_chunks(snapshot, ())) == 0


def test_source_selection_preserves_tuple_fixture_compatibility(tmp_path):
    from dataclasses import replace

    snapshot = _notes(tmp_path, (b"# One\nfirst\n", b"# Two\nsecond\n"))
    rows = tuple(snapshot.chunks)
    fixture = replace(snapshot, chunks=rows)
    assert corpus.select_snapshot_chunks(fixture, (rows[1].source_path,)) == rows[1:]
