"""Helpers shared by test_vectors_stream_through_build_reuse_and_read.py and the test files importing them."""

import os

import search_memory as search

from tests.generation_rebuild_reuse_helpers import (
    _CountingEmbedder,
    _seal_like_a_published_generation,
    _snapshot_from,
)


def _windows_acl_snapshot(path):
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    owner = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
    control, _revision = descriptor.GetSecurityDescriptorControl()
    acl = descriptor.GetSecurityDescriptorDacl()
    assert acl is not None, "NULL DACL grants unrestricted access"
    entries = tuple(_windows_ace(acl.GetAce(index)) for index in range(acl.GetAceCount()))
    return owner, bool(control & win32security.SE_DACL_PROTECTED), entries


def _windows_ace(ace):
    import win32security

    assert len(ace) == 3, "unsupported ACE shape"
    (kind, flags), mask, sid = ace
    return kind, flags, mask, win32security.ConvertSidToStringSid(sid)


def _owner_can_use_copy(entry, owner, current_sid):
    _kind, flags, mask, sid = entry
    identities = {owner, current_sid, "S-1-3-4"}
    return sid in identities and mask & 0x001F01FF == 0x001F01FF and not flags & 0x08


def _assert_windows_private_acl(owner, protected, entries, current_sid, require_protected):
    assert owner in {current_sid, "S-1-5-32-544"}
    assert protected or not require_protected
    assert entries
    allowed = {current_sid, "S-1-3-4", "S-1-5-18", "S-1-5-32-544"}
    assert all(entry[0] == 0 and entry[3] in allowed for entry in entries)
    assert any(_owner_can_use_copy(entry, owner, current_sid) for entry in entries)


def _assert_private_copy(captured):
    if os.name != "nt":
        assert captured.parent.stat().st_mode & 0o777 == 0o700
        return
    from operational_ownership import current_actor_identity

    actor = current_actor_identity()
    assert actor.startswith("windows-sid:")
    sid = actor.removeprefix("windows-sid:")
    _assert_windows_private_acl(*_windows_acl_snapshot(captured.parent), sid, True)
    _assert_windows_private_acl(*_windows_acl_snapshot(captured), sid, False)


def _parent(tmp_path):
    snapshot = _snapshot_from(tmp_path / "vault", {"page.md": "# H\nx\n" * 257})
    directory = tmp_path / "parent"
    directory.mkdir()
    search.build_generation_numpy_vectors(
        snapshot, directory, embedder=_CountingEmbedder(), model_id="fixture/model",
        model_revision="rev-1", dimensions=4,
    )
    _seal_like_a_published_generation(directory)
    return snapshot, directory
