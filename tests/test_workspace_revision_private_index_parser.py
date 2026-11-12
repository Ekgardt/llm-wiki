"""The private index parser rejects what it cannot prove, honours cancellation, and keeps git's tracked-path status exact."""


from __future__ import annotations

import hashlib
import os
import struct
import time
from pathlib import Path

import pytest
import workspace_revision
from repository_scope import resolve_repository_scope
from workspace_revision import (
    compute_workspace_revision,
)

from tests.slow_machine import SHORT_TIMEOUT
from tests.workspace_revision_helpers import (
    _isolated_global_git_configuration,  # noqa: F401
    _linux_memfd_available,
    _run_git,
    _symlink_or_skip,
    _verification_git_call_counts,
)


def _rechecksum_index(content: bytes, hash_name: str = "sha1") -> bytes:
    digest_size = hashlib.new(hash_name).digest_size
    body = content[:-digest_size]
    return body + hashlib.new(hash_name, body).digest()


def _append_index_extension(
    content: bytes,
    signature: bytes,
    payload: bytes = b"",
    *,
    hash_name: str = "sha1",
) -> bytes:
    digest_size = hashlib.new(hash_name).digest_size
    body = content[:-digest_size] + signature + struct.pack("!I", len(payload)) + payload
    return body + hashlib.new(hash_name, body).digest()


def _index_entries_end(content: bytes, hash_name: str = "sha1") -> int:
    hash_size = hashlib.new(hash_name).digest_size
    count = struct.unpack_from("!I", content, 8)[0]
    offset = 12
    for _entry in range(count):
        fixed_end = offset + 40 + hash_size + 2
        path_end = content.index(b"\0", fixed_end)
        offset += ((path_end + 1 - offset + 7) // 8) * 8
    return offset


def _status_header_records_only(status: bytes) -> bytes:
    """`status` with every path record dropped and only its headers kept.

    Used to forge a private status that reports no path at all, which must not be
    read as agreement with an exact status that reports a deletion.
    """
    return b"\0".join(record for record in status.split(b"\0") if record.startswith(b"#"))


def test_private_index_parser_rejects_malformed_binary_indexes(repository: Path) -> None:
    raw = (repository / ".git/index").read_bytes()
    malformed_checksum = raw[:-1] + bytes([raw[-1] ^ 1])
    malformed_header = bytearray(raw)
    malformed_header[:4] = b"NOPE"
    malformed_count = bytearray(raw)
    struct.pack_into("!I", malformed_count, 8, struct.unpack_from("!I", raw, 8)[0] + 1)
    malformed_path = bytearray(raw)
    path_offset = malformed_path.find(b"pkg/api.py\0")
    assert path_offset >= 0
    malformed_path[path_offset] = ord("/")
    malformed_terminator = bytearray(raw)
    malformed_terminator[path_offset + len(b"pkg/api.py")] = ord("x")
    cases = (
        malformed_checksum,
        _rechecksum_index(bytes(malformed_header)),
        _rechecksum_index(bytes(malformed_count)),
        _rechecksum_index(bytes(malformed_path)),
        _rechecksum_index(bytes(malformed_terminator)),
        raw[:-1],
    )

    for content in cases:
        assert workspace_revision.parse_git_index(
            content,
            hash_name="sha1",
            deadline=time.monotonic() + SHORT_TIMEOUT,
            cancelled=None,
        ) is None


def test_private_index_checksum_cancellation_is_bounded_by_hash_chunks(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = (repository / ".git/index").read_bytes()
    large = _append_index_extension(raw, b"TREE", b"x" * (4 * 1024 * 1024))
    real_new = hashlib.new
    updates = 0

    class ThrottledHash:
        def __init__(self, hash_name: str) -> None:
            self.backing = real_new(hash_name)
            self.digest_size = self.backing.digest_size

        def update(self, content) -> None:
            nonlocal updates
            time.sleep(len(content) / (8 * 1024 * 1024))
            self.backing.update(content)
            updates += 1

        def digest(self) -> bytes:
            return self.backing.digest()

    def throttled_new(hash_name: str, content=b""):
        result = ThrottledHash(hash_name)
        if content:
            result.update(content)
        return result

    monkeypatch.setattr(workspace_revision.hashlib, "new", throttled_new)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="cancelled"):
        workspace_revision.parse_git_index(
            large,
            hash_name="sha1",
            deadline=None,
            cancelled=lambda: updates > 0,
        )
    elapsed = time.monotonic() - started

    assert updates == 1
    assert elapsed < 0.25


def test_private_index_rebuild_honors_cancellation_before_cpu_loop(repository: Path) -> None:
    parsed = workspace_revision.parse_git_index(
        (repository / ".git/index").read_bytes(),
        hash_name="sha1",
        deadline=None,
        cancelled=None,
    )
    assert parsed is not None
    expanded = type(parsed)(
        parsed.content,
        parsed.entries_end,
        parsed.checksum_offset,
        parsed.entries * 10_000,
    )

    with pytest.raises(TimeoutError, match="cancelled"):
        workspace_revision.refresh_private_index(
            expanded,
            {},
            hash_name="sha1",
            deadline=None,
            cancelled=lambda: True,
        )


def test_private_index_parser_rejects_unsupported_extensions_and_modes(
    repository: Path,
) -> None:
    raw = (repository / ".git/index").read_bytes()
    for signature in (b"link", b"sdir", b"abcd", b"ABCD", b"UNTR", b"FSMN"):
        assert workspace_revision.parse_git_index(
            _append_index_extension(raw, signature),
            hash_name="sha1",
            deadline=time.monotonic() + SHORT_TIMEOUT,
            cancelled=None,
        ) is None

    nonregular = bytearray(raw)
    struct.pack_into("!I", nonregular, 12 + 24, 0o120000)
    assert workspace_revision.parse_git_index(
        _rechecksum_index(bytes(nonregular)),
        hash_name="sha1",
        deadline=time.monotonic() + SHORT_TIMEOUT,
        cancelled=None,
    ) is None
    zero_oid = bytearray(raw)
    zero_oid[12 + 40 : 12 + 60] = b"\0" * 20
    assert workspace_revision.parse_git_index(
        _rechecksum_index(bytes(zero_oid)),
        hash_name="sha1",
        deadline=time.monotonic() + SHORT_TIMEOUT,
        cancelled=None,
    ) is None


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_memfd_strips_every_optional_extension(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = repository / ".git/index"
    raw = index.read_bytes()
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    expected_head, expected_status = workspace_revision.git_state(
        repository,
        allow_missing_head=False,
        deadline=None,
        cancelled=None,
    )
    entries_end = _index_entries_end(raw)
    extension_free = raw[:entries_end]
    extension_free += hashlib.sha1(extension_free).digest()
    index.write_bytes(_append_index_extension(extension_free, b"TREE", b"untrusted-cache"))
    inspected = False

    def inspect_private_index(_root: Path, descriptor: int, **_kwargs):
        nonlocal inspected
        content = os.pread(descriptor, 64 * 1024 * 1024, 0)
        private_entries_end = _index_entries_end(content)
        inspected = True
        assert len(content) == private_entries_end + hashlib.sha1().digest_size
        assert hashlib.sha1(content[:private_entries_end]).digest() == content[private_entries_end:]
        return expected_head, expected_status

    monkeypatch.setattr(
        workspace_revision,
        "git_state_with_private_index",
        inspect_private_index,
    )

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert inspected


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_invalidates_every_non_content_proven_entry(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    irrelevant = repository / "README.txt"
    irrelevant.write_text("tracked but outside revision content\n", encoding="utf-8")
    _run_git(repository, "add", "README.txt")
    _run_git(repository, "commit", "-m", "track irrelevant file")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    expected_head, expected_status = workspace_revision.git_state(
        repository,
        allow_missing_head=False,
        deadline=None,
        cancelled=None,
    )
    inspected = False

    def inspect_private_index(_root: Path, descriptor: int, **_kwargs):
        nonlocal inspected
        content = os.pread(descriptor, 64 * 1024 * 1024, 0)
        parsed = workspace_revision.parse_git_index(
            content,
            hash_name="sha1",
            deadline=None,
            cancelled=None,
        )
        assert parsed is not None
        entry = next(item for item in parsed.entries if item.path == "README.txt")
        assert struct.unpack_from("!6I", content, entry.offset) == (0,) * 6
        assert struct.unpack_from("!3I", content, entry.offset + 28) == (0,) * 3
        inspected = True
        return expected_head, expected_status

    monkeypatch.setattr(
        workspace_revision,
        "git_state_with_private_index",
        inspect_private_index,
    )

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert inspected


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_detects_mutated_tracked_file_outside_relevance_set(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    irrelevant = repository / "README.txt"
    irrelevant.write_text("before\n", encoding="utf-8")
    _run_git(repository, "add", "README.txt")
    _run_git(repository, "commit", "-m", "track irrelevant file")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    irrelevant.write_text("after\n", encoding="utf-8")

    with monkeypatch.context() as forced:
        forced.setattr(workspace_revision, "try_private_git_state", lambda *args, **kwargs: None)
        exact = workspace_revision.verify_workspace_revision_unchanged(scope, expected)
    optimized, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert exact is False
    assert optimized is False
    assert exact_calls == 0
    assert private_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_large_unmatched_tracked_asset_uses_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = repository / "large-asset.bin"
    with asset.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024)
    _run_git(repository, "add", "large-asset.bin")
    _run_git(repository, "commit", "-m", "add large tracked asset")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux tracked path semantics")
def test_private_index_nonregular_unmatched_tracked_path_uses_exact_fallback(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracked = repository / "README.txt"
    tracked.write_text("tracked\n", encoding="ascii")
    _run_git(repository, "add", "README.txt")
    _run_git(repository, "commit", "-m", "add tracked asset")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    outside = tmp_path / "outside.txt"
    outside.write_text("replacement\n", encoding="ascii")
    tracked.unlink()
    _symlink_or_skip(tracked, outside)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    # A status path a compute would drop -- this one is a symlink in no relevant
    # place -- is not evidence of a change, or every later verification would
    # answer "changed" for ever:
    # docs/research/2026-09-17-a-revision-walks-what-the-corpus-walks.md
    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_preserves_exact_tracked_deletion_status(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted = repository / "pkg/base.py"
    deleted.unlink()
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    exact_head, exact_status = workspace_revision.git_state(
        repository,
        allow_missing_head=False,
        deadline=None,
        cancelled=None,
    )

    assert ("pkg/base.py", "deleted") in workspace_revision.status_paths(exact_status)
    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert (exact_head, result, exact_calls, private_calls) == (expected.git_head, True, 0, 1)


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize("reported", ["missing", "modified"])
def test_private_index_rejects_deleted_path_status_disagreement(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported: str,
) -> None:
    (repository / "pkg/base.py").unlink()
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    _head, exact_status = workspace_revision.git_state(
        repository,
        allow_missing_head=False,
        deadline=None,
        cancelled=None,
    )
    if reported == "missing":
        private_status = _status_header_records_only(exact_status)
    else:
        private_status = exact_status.replace(b"1 .D ", b"1 .M ", 1)
        assert private_status != exact_status
    monkeypatch.setattr(
        workspace_revision,
        "git_state_with_private_index",
        lambda *args, **kwargs: (expected.git_head, private_status),
    )

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is False


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_matches_exact_staged_delete_with_restored_untracked_path(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_git(repository, "rm", "--cached", "pkg/base.py")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    assert next(entry for entry in expected.entries if entry.path == "pkg/base.py").kind == "untracked"
    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 0
    assert private_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux index variants")
def test_private_index_parser_rejects_v4_split_and_colliding_paths(
    tmp_path: Path,
) -> None:
    roots = []
    for name in ("v4", "split", "collision"):
        root = tmp_path / name
        root.mkdir()
        _run_git(root, "init", "-q", "-b", "main")
        _run_git(root, "config", "user.name", "Revision Test")
        _run_git(root, "config", "user.email", "revision@example.invalid")
        (root / "a.py").write_text("a = 1\n", encoding="utf-8")
        if name == "collision":
            (root / "A.py").write_text("A = 1\n", encoding="utf-8")
        _run_git(root, "add", "--all")
        _run_git(root, "commit", "-m", "fixture")
        roots.append(root)
    _run_git(roots[0], "update-index", "--index-version", "4")
    _run_git(roots[1], "update-index", "--split-index")

    for root in roots:
        assert workspace_revision.parse_git_index(
            (root / ".git/index").read_bytes(),
            hash_name="sha1",
            deadline=time.monotonic() + SHORT_TIMEOUT,
            cancelled=None,
        ) is None
