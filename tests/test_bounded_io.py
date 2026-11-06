from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

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


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows has no root-owned system symlink; reparse points stay refused",
)
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


@pytest.mark.skipif(os.name != "nt", reason="requires Windows reparse points")
def test_windows_reparse_point_ancestor_is_refused(tmp_path, monkeypatch) -> None:
    """The exception is root ownership, which Windows cannot express here.

    `_system_symlink` returns False on Windows by construction, and the reparse
    attribute is refused before the symlink question is even asked.
    """
    import bounded_io

    real = tmp_path / "real"
    real.mkdir()
    (real / "page.md").write_bytes(b"inside")
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    monkeypatch.setattr(bounded_io, "_system_symlink", lambda candidate: True)

    with pytest.raises(PermissionError, match="parent must be a regular directory"):
        bounded_io.read_stable_bytes(link / "page.md", 1024, label="linked target")


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


class _FakeClock:
    """A wall clock that moves only when the reader sleeps, or when told to."""

    def __init__(self, now_ns: int, *, coarse_sleeps: int = 0) -> None:
        self.now_ns = now_ns
        self.sleeps: list[float] = []
        self._coarse_sleeps = coarse_sleeps

    def time_ns(self) -> int:
        return self.now_ns

    def monotonic(self) -> float:
        return sum(self.sleeps)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) > self._coarse_sleeps:
            self.now_ns += int(seconds * 1_000_000_000)


def _install(monkeypatch: pytest.MonkeyPatch, clock: _FakeClock) -> None:
    import bounded_io

    monkeypatch.setattr(bounded_io.time, "time_ns", clock.time_ns)
    monkeypatch.setattr(bounded_io.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(bounded_io.time, "sleep", clock.sleep)


def _stamped(tmp_path: Path) -> tuple[Path, int]:
    import bounded_io

    target = tmp_path / "page.md"
    target.write_bytes(b"settled")
    return target, bounded_io.change_stamp_ns(target.lstat())


@pytest.mark.skipif(
    os.name == "nt", reason="Windows os.stat has no change time; st_ctime is the creation time"
)
def test_stable_read_rejects_same_size_rewrite_that_keeps_its_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Equal size and mtime on purpose: only the change time can tell the rewrite."""
    import bounded_io

    target = tmp_path / "page.md"
    target.write_bytes(b"version 1")
    stamps = target.stat()
    real_open = os.open

    def rewrite_before_open(
        path: str | os.PathLike[str], flags: int, *args: Any, **kwargs: Any
    ) -> int:
        if Path(path) == target:
            target.write_bytes(b"version 2")
            os.utime(target, ns=(stamps.st_atime_ns, stamps.st_mtime_ns))
            assert (target.stat().st_size, target.stat().st_mtime_ns) == (
                stamps.st_size,
                stamps.st_mtime_ns,
            )
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(bounded_io.os, "open", rewrite_before_open)

    with pytest.raises(bounded_io.SourceChangedDuringRead, match="changed before open"):
        bounded_io.read_stable_bytes(target, 1024, label="page")


def test_stable_read_waits_until_a_fresh_file_is_a_tick_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bounded_io

    target, stamped_ns = _stamped(tmp_path)
    clock = _FakeClock(stamped_ns)
    _install(monkeypatch, clock)

    assert bounded_io.read_stable_bytes(target, 1024) == b"settled"
    assert clock.sleeps == [bounded_io.TIMESTAMP_SETTLE_NS / 1_000_000_000]


def test_a_clock_coarser_than_the_window_costs_another_look_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows' clock before Python 3.13 can stand still through one whole wait."""
    import bounded_io

    target, stamped_ns = _stamped(tmp_path)
    clock = _FakeClock(stamped_ns, coarse_sleeps=2)
    _install(monkeypatch, clock)

    assert bounded_io.settled_lstat(target).st_ino == target.lstat().st_ino
    assert len(clock.sleeps) == 3


def _append_while_waiting(
    monkeypatch: pytest.MonkeyPatch, target: Path, appends: int
) -> _FakeClock:
    """The first `appends` waits each see an append landing at that moment."""
    import bounded_io

    target.write_bytes(b"settled")
    clock = _FakeClock(bounded_io.change_stamp_ns(target.lstat()))
    _install(monkeypatch, clock)

    def append_then_wait(seconds: float) -> None:
        clock.sleep(seconds)
        if len(clock.sleeps) > appends:
            return
        with target.open("ab") as handle:
            handle.write(b"+")
        clock.now_ns = bounded_io.change_stamp_ns(target.lstat())

    monkeypatch.setattr(bounded_io.time, "sleep", append_then_wait)
    return clock


def test_a_burst_of_writes_is_read_once_it_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Appends faster than the window, then quiet: the reader waits them out."""
    import bounded_io

    target = tmp_path / "page.md"
    clock = _append_while_waiting(monkeypatch, target, appends=5)

    assert bounded_io.read_stable_bytes(target, 1024) == b"settled+++++"
    assert len(clock.sleeps) == 6


def test_a_file_that_never_stops_being_written_is_refused_after_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bounded_io

    target = tmp_path / "page.md"
    clock = _append_while_waiting(monkeypatch, target, appends=10**6)

    with pytest.raises(bounded_io.SourceChangedDuringRead, match="still being written"):
        bounded_io.read_stable_bytes(target, 1024 * 1024, label="page")
    assert sum(clock.sleeps) * 1_000_000_000 <= bounded_io.SETTLE_BUDGET_NS
    assert len(clock.sleeps) == bounded_io.SETTLE_BUDGET_NS // bounded_io.TIMESTAMP_SETTLE_NS - 1


@pytest.mark.parametrize(
    ("ahead_ns", "sleeps"),
    [(20_000_000, 1), (20_000_001, 0)],
    ids=["one-tick-ahead-is-the-same-tick", "more-than-a-tick-ahead-cannot-be-written"],
)
def test_only_a_stamp_within_a_tick_of_the_clock_is_waited_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ahead_ns: int, sleeps: int
) -> None:
    import bounded_io

    assert ahead_ns - 1 <= bounded_io.TIMESTAMP_SETTLE_NS <= ahead_ns
    target, stamped_ns = _stamped(tmp_path)
    clock = _FakeClock(stamped_ns - ahead_ns)
    _install(monkeypatch, clock)

    assert bounded_io.settled_lstat(target, label="page").st_ino == target.lstat().st_ino
    assert len(clock.sleeps) == sleeps


def test_a_page_with_a_future_mtime_is_read(tmp_path: Path) -> None:
    """A synced or `touch -d` mtime an hour ahead: no write now can carry it."""
    import bounded_io

    target = tmp_path / "page.md"
    target.write_bytes(b"from a fast clock")
    future_ns = time.time_ns() + 3600 * 1_000_000_000
    os.utime(target, ns=(future_ns, future_ns))

    assert bounded_io.read_stable_bytes(target, 1024) == b"from a fast clock"


@pytest.mark.skipif(
    os.name == "nt", reason="Windows os.stat has no change time; st_ctime is the creation time"
)
def test_a_same_size_rewrite_of_a_future_stamped_page_is_still_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bounded_io

    target = tmp_path / "page.md"
    target.write_bytes(b"version 1")
    future_ns = time.time_ns() + 3600 * 1_000_000_000
    os.utime(target, ns=(future_ns, future_ns))
    real_open = os.open

    def rewrite_before_open(
        path: str | os.PathLike[str], flags: int, *args: Any, **kwargs: Any
    ) -> int:
        if Path(path) == target:
            target.write_bytes(b"version 2")
            os.utime(target, ns=(future_ns, future_ns))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(bounded_io.os, "open", rewrite_before_open)

    with pytest.raises(bounded_io.SourceChangedDuringRead, match="changed before open"):
        bounded_io.read_stable_bytes(target, 1024, label="page")


def test_a_wait_that_would_outlast_the_deadline_times_out_without_sleeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bounded_io

    target, stamped_ns = _stamped(tmp_path)
    clock = _FakeClock(stamped_ns)
    _install(monkeypatch, clock)
    monkeypatch.setattr(bounded_io.time, "monotonic", lambda: 100.0)

    with pytest.raises(TimeoutError, match="deadline"):
        bounded_io.read_stable_bytes(target, 1024, deadline=100.01)
    assert clock.sleeps == []
