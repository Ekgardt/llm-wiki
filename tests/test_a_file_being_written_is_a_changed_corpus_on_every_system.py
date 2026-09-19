"""A source written while it is read by path is a changed corpus, not an unsafe path.

Where directory descriptors are not available the collector seals a file by path
and reads it later. A writer in between used to surface as a bare
`PermissionError`, which is neither retried nor named as a change.

Research: `docs/research/2026-09-17-five-failures-only-the-other-systems-showed.md`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bounded_io  # noqa: E402
import corpus_snapshot  # noqa: E402


def _source(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path.resolve() / "vault"
    source = vault / "pkg" / "alpha.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"def alpha():\n    return 1\n")
    return vault, source


def _read_with_a_writer_in_between(path: Path, max_bytes: int, *, label: str) -> bytes:
    """`read_stable_bytes`, with the write that loses it the race put in the gap."""
    before = path.lstat()
    path.write_bytes(path.read_bytes() + b"# written meanwhile\n")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        return bounded_io._read_open_descriptor(
            descriptor, path, bounded_io._file_identity(before), max_bytes, label, None
        )
    finally:
        os.close(descriptor)


def test_a_file_written_between_the_look_and_the_open_is_named_as_changed(tmp_path):
    _vault, source = _source(tmp_path)

    with pytest.raises(bounded_io.SourceChangedDuringRead) as refused:
        _read_with_a_writer_in_between(source, 1024, label="source")

    # Still a PermissionError: every caller that refused before refuses now.
    assert isinstance(refused.value, PermissionError)


def test_a_file_replaced_under_the_reader_is_named_as_changed(tmp_path):
    _vault, source = _source(tmp_path)
    identity = bounded_io._file_identity(source.lstat())
    newer = source.with_name("newer.py")
    newer.write_bytes(source.read_bytes())
    os.replace(newer, source)

    with pytest.raises(bounded_io.SourceChangedDuringRead):
        bounded_io._require_same_file_at_path(source, identity, "source")


def test_the_collector_calls_that_a_changed_corpus(tmp_path, monkeypatch):
    _vault, source = _source(tmp_path)
    monkeypatch.setattr(corpus_snapshot, "read_stable_bytes", _read_with_a_writer_in_between)

    with pytest.raises(corpus_snapshot.CorpusChanged):
        corpus_snapshot._sealed_source_bytes(source, 1024, "source")


def test_an_unsafe_path_is_still_refused_and_not_retried(tmp_path):
    vault, source = _source(tmp_path)
    link = vault / "pkg" / "link.py"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("this system does not let the test make a symlink")

    with pytest.raises(PermissionError) as refused:
        corpus_snapshot._sealed_source_bytes(link, 1024, "source")

    assert not isinstance(refused.value, bounded_io.SourceChangedDuringRead)


def test_a_file_written_while_it_is_sealed_is_a_changed_corpus(tmp_path, monkeypatch):
    vault, source = _source(tmp_path)
    real_seal = corpus_snapshot._build_path_seal

    def seal_then_write(*args, **kwargs):
        seal = real_seal(*args, **kwargs)
        source.write_bytes(source.read_bytes() + b"# written meanwhile\n")
        return seal

    monkeypatch.setattr(corpus_snapshot, "_build_path_seal", seal_then_write)

    with pytest.raises(corpus_snapshot.CorpusChanged):
        corpus_snapshot._seal_source_file(vault, source, 8)


def test_a_quiet_file_is_sealed_down_to_itself(tmp_path):
    vault, source = _source(tmp_path)

    seal = corpus_snapshot._seal_source_file(vault, source, 8)

    assert [identity.path for identity in seal] == [vault, vault / "pkg", source]
