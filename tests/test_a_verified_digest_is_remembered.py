"""A digest already verified is remembered by stat identity, Git's way.

Hashing every artifact of an immutable generation in every new process is what a
cold answer paid. What is remembered, and what must still be hashed, is the
whole contract here. Research:
`docs/research/2026-09-12-a-verdict-worth-remembering-across-processes.md`.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import verified_artifacts  # noqa: E402


def _artifact(tmp_path: Path, content: bytes = b"payload") -> Path:
    path = tmp_path / "evidence.sqlite3"
    path.write_bytes(content)
    return path


def _aged(path: Path, seconds: float) -> os.stat_result:
    """Put the file's mtime in the past, as an artifact published earlier is."""
    when = time.time() - seconds
    os.utime(path, (when, when))
    return path.stat()


def _remembering_cache(tmp_path: Path, path: Path, digest: str):
    cache = verified_artifacts.VerifiedArtifacts(tmp_path)
    cache.remember("generation-1", "evidence.sqlite3", _aged(path, 60), digest)
    cache.save()
    return verified_artifacts.VerifiedArtifacts(tmp_path)


def test_the_same_bytes_are_remembered_and_not_hashed_again(tmp_path):
    path = _artifact(tmp_path)

    reopened = _remembering_cache(tmp_path, path, "abc")

    assert reopened.remembered("generation-1", "evidence.sqlite3", path.stat()) == "abc"


def test_a_file_that_changed_is_not_remembered(tmp_path):
    path = _artifact(tmp_path)
    reopened = _remembering_cache(tmp_path, path, "abc")

    path.write_bytes(b"payload and more")

    assert reopened.remembered("generation-1", "evidence.sqlite3", path.stat()) is None


def test_an_artifact_as_new_as_the_cache_is_racily_clean(tmp_path):
    """Git's rule: a file changed inside the same tick would otherwise pass."""
    path = _artifact(tmp_path)
    reopened = _remembering_cache(tmp_path, path, "abc")
    now = time.time()
    os.utime(path, (now, now))

    assert reopened.remembered("generation-1", "evidence.sqlite3", path.stat()) is None


def test_another_generation_with_the_same_path_is_not_remembered(tmp_path):
    path = _artifact(tmp_path)
    reopened = _remembering_cache(tmp_path, path, "abc")

    assert reopened.remembered("generation-2", "evidence.sqlite3", path.stat()) is None


def test_a_corrupt_cache_is_an_empty_cache(tmp_path):
    path = _artifact(tmp_path)
    _remembering_cache(tmp_path, path, "abc")
    verified_artifacts.cache_path(tmp_path).write_bytes(b"{ not json")

    cache = verified_artifacts.VerifiedArtifacts(tmp_path)

    assert cache.remembered("generation-1", "evidence.sqlite3", path.stat()) is None


def test_nothing_is_written_when_nothing_new_was_verified(tmp_path):
    path = _artifact(tmp_path)
    _remembering_cache(tmp_path, path, "abc")
    cache_file = verified_artifacts.cache_path(tmp_path)
    before = cache_file.stat().st_mtime_ns

    verified_artifacts.VerifiedArtifacts(tmp_path).save()

    assert cache_file.stat().st_mtime_ns == before


def test_the_cache_keeps_a_bounded_number_of_entries(tmp_path):
    path = _artifact(tmp_path)
    metadata = _aged(path, 60)
    cache = verified_artifacts.VerifiedArtifacts(tmp_path)
    for index in range(verified_artifacts.MAX_ENTRIES + 8):
        cache.remember(f"generation-{index}", "evidence.sqlite3", metadata, "abc")
    cache.save()

    reopened = verified_artifacts.VerifiedArtifacts(tmp_path)

    assert len(reopened.entries) == verified_artifacts.MAX_ENTRIES


def test_the_format_receipt_key_comes_from_the_remembered_digest(tmp_path):
    """0.32 s of a cold answer was hashing 241 MB to look up a stored verdict."""
    import evidence_graph

    generation = tmp_path / "cache/evidence-graph/generations/generation-7"
    generation.mkdir(parents=True)
    database = generation / "evidence.sqlite3"
    database.write_bytes(b"payload")
    cache = verified_artifacts.VerifiedArtifacts(tmp_path)
    cache.remember("generation-7", "evidence.sqlite3", _aged(database, 60), "known")
    cache.save()

    remembered = evidence_graph._remembered_artifact_digest(  # noqa: SLF001
        verified_artifacts.VerifiedArtifacts(tmp_path), database
    )

    assert remembered == "known"


def test_a_moved_artifact_has_no_remembered_digest(tmp_path):
    import evidence_graph

    generation = tmp_path / "cache/evidence-graph/generations/generation-7"
    generation.mkdir(parents=True)
    database = generation / "evidence.sqlite3"
    database.write_bytes(b"payload")
    cache = verified_artifacts.VerifiedArtifacts(tmp_path)
    cache.remember("generation-7", "evidence.sqlite3", _aged(database, 60), "known")
    cache.save()
    database.write_bytes(b"payload changed")

    remembered = evidence_graph._remembered_artifact_digest(  # noqa: SLF001
        verified_artifacts.VerifiedArtifacts(tmp_path), database
    )

    assert remembered is None


def test_a_shallow_index_check_does_not_walk_every_row():
    """The rows belong to the deep check; a read has the digest."""
    import search_memory

    class _Refuses:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("a read must not walk the index rows")

    assert (
        search_memory._stored_chunks_match(  # noqa: SLF001
            _Refuses(), None, count=3, deadline=None, cancelled=None
        )
        is True
    )
