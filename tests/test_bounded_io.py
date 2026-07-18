from __future__ import annotations

from pathlib import Path

import pytest


def test_parent_swap_to_outside_target_is_rejected_before_bytes_return(
    tmp_path, monkeypatch
):
    import bounded_io

    checked_parent = tmp_path / "checked"
    moved_parent = tmp_path / "checked-original"
    outside = tmp_path / "outside"
    checked_parent.mkdir()
    outside.mkdir()
    target = checked_parent / "page.md"
    target.write_bytes(b"inside")
    (outside / "page.md").write_bytes(b"outside-secret")
    real_open = bounded_io.os.open
    swapped = False

    def swap_parent_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == target and not swapped:
            checked_parent.rename(moved_parent)
            swapped = True
            # Model path resolution after a parent symlink/reparse swap without
            # requiring symlink privileges on Windows.
            return real_open(outside / "page.md", flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(bounded_io.os, "open", swap_parent_before_open)

    with pytest.raises(PermissionError, match="changed before open"):
        bounded_io.read_stable_bytes(target, 1024, label="race target")
    assert swapped is True
