"""A generation artifact is hashed once, and any move of it is still caught.

One query seals the same generation four or five times. Each seal used to
re-hash every artifact; measured on the live vault that was 0.34-2.77 s of a
10 s MCP budget for a verdict that cannot change between the first call and the
fifth. The seal now reuses a checksum it already earned, but only for the exact
same inode at the exact same size and stamps -- these tests hold that line.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import search_memory  # noqa: E402


def _settled_artifact(tmp_path: Path, body: bytes = b"artifact-bytes") -> Path:
    """Written, then left alone long enough to be past the racily-clean window."""
    path = tmp_path / "search.sqlite3"
    path.write_bytes(body)
    time.sleep(0.05)
    return path


@pytest.fixture(autouse=True)
def _clean_observations():
    search_memory._reset_seal_observations()
    yield
    search_memory._reset_seal_observations()


@pytest.fixture()
def hashes(monkeypatch):
    counted: list[str] = []
    real = search_memory._hashed_seal

    def counting(path, before, expected, **stop):
        counted.append(path.name)
        return real(path, before, expected, **stop)

    monkeypatch.setattr(search_memory, "_hashed_seal", counting)
    return counted


# The cache exists only where `st_ctime_ns` is a change time. Windows stamps
# creation time there, which survives an in-place rewrite, so a forged mtime at
# the same size would be served unchecked — the cache is deliberately inert on
# such a host. Measured 2026-08-30: this was found by the Windows jobs, and
# `test_a_rewrite_that_forges_mtime_is_still_caught_by_ctime` is what found it.
CACHE_IS_TRUSTWORTHY = search_memory._CTIME_IS_A_CHANGE_TIME


def test_nothing_is_cached_where_ctime_is_not_a_change_time(tmp_path, hashes):
    """The guard is the whole warrant for the cache; without it, no cache."""
    if CACHE_IS_TRUSTWORTHY:
        pytest.skip("this host stamps a change time, so the cache is warranted")
    path = _settled_artifact(tmp_path)

    search_memory._sealed_file(path)
    search_memory._sealed_file(path)

    assert hashes == ["search.sqlite3", "search.sqlite3"]


@pytest.mark.skipif(
    not CACHE_IS_TRUSTWORTHY,
    reason="no change time on this host, so the cache is inert by design",
)
def test_an_unchanged_artifact_is_hashed_once_and_seals_the_same(tmp_path, hashes):
    path = _settled_artifact(tmp_path)

    first = search_memory._sealed_file(path)
    repeats = [search_memory._sealed_file(path) for _ in range(4)]

    assert hashes == ["search.sqlite3"], "the seal re-read bytes it had already hashed"
    assert repeats == [first] * 4


def test_an_in_place_rewrite_of_the_same_size_is_still_caught(tmp_path, hashes):
    path = _settled_artifact(tmp_path, b"aaaaaaaa")
    before = search_memory._sealed_file(path)

    path.write_bytes(b"bbbbbbbb")
    after = search_memory._sealed_file(path)

    assert len(hashes) == 2
    assert after != before
    assert after[-1] != before[-1], "same size must not reuse the earlier checksum"


def test_a_rewrite_that_forges_mtime_is_still_caught_by_ctime(tmp_path, hashes):
    """No syscall can set ctime: the kernel stamps it on every inode change."""
    path = _settled_artifact(tmp_path, b"aaaaaaaa")
    before = search_memory._sealed_file(path)
    status = path.stat()
    stamps = (status.st_atime, status.st_mtime)

    path.write_bytes(b"bbbbbbbb")
    os.utime(path, stamps)
    after = search_memory._sealed_file(path)

    assert path.stat().st_mtime == stamps[1], "the test failed to forge mtime"
    assert len(hashes) == 2
    assert after[-1] != before[-1]


def test_a_replaced_file_is_caught_even_at_the_same_size(tmp_path, hashes):
    path = _settled_artifact(tmp_path, b"aaaaaaaa")
    before = search_memory._sealed_file(path)

    other = tmp_path / "other"
    other.write_bytes(b"bbbbbbbb")
    os.replace(other, path)
    after = search_memory._sealed_file(path)

    assert len(hashes) == 2
    assert after[-1] != before[-1]


def test_a_just_written_artifact_is_not_remembered(tmp_path, hashes):
    """Git's racily-clean rule: a stamp no older than the read proves nothing."""
    path = tmp_path / "search.sqlite3"
    path.write_bytes(b"fresh")

    search_memory._sealed_file(path)
    search_memory._sealed_file(path)

    assert len(hashes) == 2, "a same-tick write would have been indistinguishable"


def test_a_different_expected_descriptor_is_hashed_again(tmp_path, hashes):
    path = _settled_artifact(tmp_path, b"aaaaaaaa")
    digest = hashlib.sha256(b"aaaaaaaa").hexdigest()

    search_memory._sealed_file(path, {"size": 8, "sha256": digest})
    search_memory._sealed_file(path, None)

    assert len(hashes) == 2, "the manifest's expectation is part of what was proved"


def test_an_artifact_that_stops_matching_its_manifest_still_raises(tmp_path):
    path = _settled_artifact(tmp_path, b"aaaaaaaa")
    digest = hashlib.sha256(b"aaaaaaaa").hexdigest()
    search_memory._sealed_file(path, {"size": 8, "sha256": digest})

    path.write_bytes(b"bbbbbbbb")

    with pytest.raises(ValueError, match="does not match active manifest"):
        search_memory._sealed_file(path, {"size": 8, "sha256": digest})


def test_the_observation_store_stays_bounded(tmp_path):
    for index in range(search_memory._SEAL_OBSERVATION_LIMIT * 2):
        path = tmp_path / f"artifact-{index}.bin"
        path.write_bytes(b"x" * (index + 1))
        time.sleep(0.002)
        search_memory._sealed_file(path)

    assert len(search_memory._seal_observations) <= search_memory._SEAL_OBSERVATION_LIMIT
