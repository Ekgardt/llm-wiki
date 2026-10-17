"""Check permission fixtures without interpreting NT Unix mode bits."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests.test_vectors_stream_through_build_reuse_and_read import (
    _assert_private_copy,
    _assert_windows_private_acl,
)

SID = "S-1-5-21-10-20-30-1001"
OWNER = (0, 3, 0x001F01FF, "S-1-3-4")
SYSTEM = (0, 3, 0x001F01FF, "S-1-5-18")
ADMINS = (0, 3, 0x001F01FF, "S-1-5-32-544")


def test_supported_temp_acl_preserves_owner_and_administrator_access():
    _assert_windows_private_acl(SID, True, (OWNER, SYSTEM, ADMINS), SID, True)
    _assert_windows_private_acl(SID, False, ((0, 0, 0x001F01FF, SID),), SID, False)


@pytest.mark.parametrize("sid", ["S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-5-21-99"])
def test_broader_read_grant_is_not_private(sid):
    with pytest.raises(AssertionError):
        _assert_windows_private_acl(SID, True, (OWNER, (0, 0, 0x00120089, sid)), SID, True)


@pytest.mark.parametrize("entries", [(), ((1, 0, 0x001F01FF, SID),),
                                    ((0, 8, 0x001F01FF, SID),),
                                    ((0, 0, 0x00120089, SID),)])
def test_missing_effective_owner_access_is_refused(entries):
    with pytest.raises(AssertionError):
        _assert_windows_private_acl(SID, True, entries, SID, True)


def test_unprotected_parent_and_foreign_owner_are_refused():
    with pytest.raises(AssertionError):
        _assert_windows_private_acl(SID, False, (OWNER,), SID, True)
    with pytest.raises(AssertionError):
        _assert_windows_private_acl("S-1-5-21-99", True, (OWNER,), SID, True)


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows DACL and pywin32")
def test_real_temp_copy_rejects_an_added_everyone_read_grant():
    from markdown_transaction import _run_acl_command

    with TemporaryDirectory(prefix="llm-wiki-acl-fixture-") as temporary:
        copied = Path(temporary) / "copy.bin"
        copied.write_bytes(b"neutral fixture")
        _assert_private_copy(copied)
        result = _run_acl_command(["icacls", str(copied), "/grant", "*S-1-1-0:(R)"])
        assert result.returncode == 0, (result.stdout, result.stderr)
        with pytest.raises(AssertionError):
            _assert_private_copy(copied)
