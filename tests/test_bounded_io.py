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


def test_stable_read_stops_at_deadline_between_bounded_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bounded_io

    target = tmp_path / "large.bin"
    target.write_bytes(b"x" * (128 * 1024))
    now = [100.0]
    real_read = bounded_io.os.read
    read_calls = 0

    def advancing_read(descriptor: int, size: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        chunk = real_read(descriptor, size)
        now[0] += 2.0
        return chunk

    monkeypatch.setattr(bounded_io.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(bounded_io.os, "read", advancing_read)

    with pytest.raises(TimeoutError, match="deadline"):
        bounded_io.read_stable_bytes(
            target,
            128 * 1024,
            label="deadline target",
            deadline=101.0,
        )
    assert read_calls == 1


def test_user_owned_symlink_ancestor_is_still_rejected(tmp_path: Path) -> None:
    """The threat model is a symlink an ordinary user can create."""
    import bounded_io

    real = tmp_path / "real"
    real.mkdir()
    (real / "page.md").write_bytes(b"inside")
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(PermissionError, match="parent must be a regular directory"):
        bounded_io.read_stable_bytes(link / "page.md", 1024, label="linked target")


def test_system_owned_symlink_ancestor_is_accepted(tmp_path, monkeypatch) -> None:
    """macOS reaches every temporary file through root's `/var` symlink."""
    import bounded_io

    real = tmp_path / "real"
    real.mkdir()
    (real / "page.md").write_bytes(b"inside")
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    monkeypatch.setattr(bounded_io, "_system_symlink", lambda candidate: candidate == link)

    assert bounded_io.read_stable_bytes(
        link / "page.md", 1024, label="linked target"
    ) == b"inside"


def test_system_symlink_requires_root_owner_and_root_owned_parent(
    tmp_path, monkeypatch
) -> None:
    import os
    import stat

    import bounded_io

    link = tmp_path / "link"
    real = tmp_path / "real"
    real.mkdir()
    try:
        link.symlink_to(real, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    if os.name == "nt":
        assert bounded_io._system_symlink(link) is False
        return

    assert bounded_io._system_symlink(link) is False  # owned by this user

    class Info:
        def __init__(self, uid: int, mode: int) -> None:
            self.st_uid = uid
            self.st_mode = mode

    root_directory = Info(0, stat.S_IFDIR | 0o755)
    open_directory = Info(0, stat.S_IFDIR | 0o777)
    monkeypatch.setattr(bounded_io.Path, "lstat", lambda _self: Info(0, 0o777))
    monkeypatch.setattr(bounded_io.Path, "stat", lambda _self: root_directory)
    assert bounded_io._system_symlink(link) is True

    monkeypatch.setattr(bounded_io.Path, "stat", lambda _self: open_directory)
    assert bounded_io._system_symlink(link) is False
