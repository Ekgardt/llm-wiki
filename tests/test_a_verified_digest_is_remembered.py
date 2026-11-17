"""A digest already verified is remembered by stat identity, Git's way.

Hashing every artifact of an immutable generation in every new process is what a
cold answer paid. What is remembered, and what must still be hashed, is the
whole contract here. Research:
`docs/research/2026-09-12-a-verdict-worth-remembering-across-processes.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import verified_artifacts  # noqa: E402

from tests.modules.verified_artifacts.cache_metadata import _aged  # noqa: E402


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

    remembered = evidence_graph.remembered_artifact_digest(  # noqa: SLF001
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

    remembered = evidence_graph.remembered_artifact_digest(  # noqa: SLF001
        verified_artifacts.VerifiedArtifacts(tmp_path), database
    )

    assert remembered is None


class _RefusesToBeWalked:
    def execute(self, *_args, **_kwargs):
        raise AssertionError("these rows were walked once already")


def test_rows_already_walked_are_not_walked_again():
    """The walk is paid once per distinct artifact digest, not once per read."""
    import generation_fts_validation

    assert (
        generation_fts_validation.stored_chunks_match(  # noqa: SLF001
            _RefusesToBeWalked(),
            None,
            count=3,
            check_rows=False,
            deadline=None,
            cancelled=None,
        )
        is True
    )


def test_a_digest_not_yet_walked_asks_for_the_walk(tmp_path):
    """A first read of these bytes walks them; the next process does not."""
    import generation_fts_validation

    manifest = {"artifacts": [{"path": "search.sqlite3", "sha256": "d" * 64}]}

    _cache, digest, first = generation_fts_validation.fts_rows_need_walking(  # noqa: SLF001
        tmp_path, manifest, False
    )
    cache, _digest, _walk = generation_fts_validation.fts_rows_need_walking(  # noqa: SLF001
        tmp_path, manifest, False
    )
    generation_fts_validation.remember_walked_rows(cache, digest, True)  # noqa: SLF001
    _again, _digest, second = generation_fts_validation.fts_rows_need_walking(  # noqa: SLF001
        tmp_path, manifest, False
    )

    assert (digest, first, second) == ("d" * 64, True, False)


def test_a_deep_check_walks_the_rows_even_when_remembered(tmp_path):
    import generation_fts_validation

    manifest = {"artifacts": [{"path": "search.sqlite3", "sha256": "e" * 64}]}
    cache, digest, _first = generation_fts_validation.fts_rows_need_walking(  # noqa: SLF001
        tmp_path, manifest, False
    )
    generation_fts_validation.remember_walked_rows(cache, digest, True)  # noqa: SLF001

    _cache, _digest, walk = generation_fts_validation.fts_rows_need_walking(  # noqa: SLF001
        tmp_path, manifest, True
    )

    assert walk is True


def test_a_manifest_without_a_digest_asks_for_the_walk(tmp_path):
    import generation_fts_validation

    _cache, digest, walk = generation_fts_validation.fts_rows_need_walking(  # noqa: SLF001
        tmp_path, {"artifacts": []}, False
    )

    assert (digest, walk) == (None, True)
