from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.module_boundaries import nonstdlib_imports as _nonstdlib_imports


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

    monkeypatch.setattr("llm_wiki.path_ancestry.system_symlink", lambda candidate: candidate == link)

    assert bounded_io.read_stable_bytes(
        link / "page.md", 1024, label="linked target"
    ) == b"inside"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows reparse points")
def test_windows_reparse_point_ancestor_is_refused(tmp_path, monkeypatch) -> None:
    """The exception is root ownership, which Windows cannot express here.

    `system_symlink` returns False on Windows by construction, and the reparse
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

    monkeypatch.setattr("llm_wiki.path_ancestry.system_symlink", lambda candidate: True)

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
        assert bounded_io.system_symlink(link) is False
        return

    assert bounded_io.system_symlink(link) is False  # owned by this user

    class Info:
        def __init__(self, uid: int, mode: int) -> None:
            self.st_uid = uid
            self.st_mode = mode

    root_directory = Info(0, stat.S_IFDIR | 0o755)
    open_directory = Info(0, stat.S_IFDIR | 0o777)
    monkeypatch.setattr(bounded_io.Path, "lstat", lambda _self: Info(0, 0o777))
    monkeypatch.setattr(bounded_io.Path, "stat", lambda _self: root_directory)
    assert bounded_io.system_symlink(link) is True

    monkeypatch.setattr(bounded_io.Path, "stat", lambda _self: open_directory)
    assert bounded_io.system_symlink(link) is False


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


@pytest.mark.parametrize(
    "deadline",
    [None, 60.0],
    ids=["no-deadline", "a-deadline-far-past-the-budget-is-not-spent"],
)
def test_a_file_that_never_stops_being_written_is_refused_after_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deadline: float | None
) -> None:
    """One busy file gives up as changed, leaving the caller's deadline to the rest."""
    import bounded_io

    target = tmp_path / "page.md"
    clock = _append_while_waiting(monkeypatch, target, appends=10**6)

    with pytest.raises(bounded_io.SourceChangedDuringRead, match="still being written"):
        bounded_io.read_stable_bytes(target, 1024 * 1024, label="page", deadline=deadline)
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


MTIME_NS = 111
CTIME_NS = 222


@pytest.mark.parametrize(
    ("system", "change_stamp", "identity_ctime"),
    [("posix", CTIME_NS, CTIME_NS), ("nt", MTIME_NS, 0)],
    ids=["posix-ctime-is-the-change-time", "windows-ctime-is-the-creation-time"],
)
def test_the_change_stamp_is_the_one_every_write_moves(
    monkeypatch: pytest.MonkeyPatch, system: str, change_stamp: int, identity_ctime: int
) -> None:
    import bounded_io

    info: Any = SimpleNamespace(
        st_dev=1, st_ino=2, st_size=3, st_mtime_ns=MTIME_NS, st_ctime_ns=CTIME_NS
    )
    monkeypatch.setattr(bounded_io.os, "name", system)

    observed = (bounded_io.change_stamp_ns(info), bounded_io.file_identity(info)[-1])

    assert observed == (change_stamp, identity_ctime)


def test_a_file_grown_while_it_is_read_is_changed_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bounded_io

    target = tmp_path / "page.md"
    target.write_bytes(b"first half")
    real_read = os.read
    appended: list[bool] = []

    def read_then_append(descriptor: int, length: int) -> bytes:
        chunk = real_read(descriptor, length)
        if not appended:
            appended.append(True)
            with target.open("ab") as handle:
                handle.write(b", second half")
        return chunk

    monkeypatch.setattr(bounded_io.os, "read", read_then_append)

    with pytest.raises(bounded_io.SourceChangedDuringRead, match="changed during read"):
        bounded_io.read_stable_bytes(target, 1024, label="page")


def test_a_parent_swapped_for_a_symlink_during_the_settle_wait_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait must not separate the ancestor check from the stat it vouches for."""
    import bounded_io

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "page.md").write_bytes(b"outside-secret")
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "page.md"
    target.write_bytes(b"inside")
    real_sleep = time.sleep
    swaps: list[str] = []

    def swap_parent_while_waiting(seconds: float) -> None:
        if not swaps:
            vault.rename(tmp_path / "vault-original")
            try:
                vault.symlink_to(outside, target_is_directory=True)
            except OSError:
                pytest.skip("directory symlinks are unavailable")
            swaps.append("swapped")
        real_sleep(seconds)

    monkeypatch.setattr(bounded_io.time, "sleep", swap_parent_while_waiting)

    with pytest.raises(PermissionError, match="parent must be a regular directory"):
        bounded_io.read_stable_bytes(target, 1024, label="page")
    assert swaps == ["swapped"], "the fresh page did not make the reader wait"


@pytest.mark.parametrize('selection', [
    slice(None), slice(2, 11), slice(-8, -1), slice(None, None, -1),
    slice(12, 1, -3), slice(1, 14, 3), slice(99, None), slice(8, 2),
])
def test_verified_range_slices_match_bytes_across_blocks(selection):
    import bounded_io

    raw = b'one\ntwo\nthree\nfour'
    with io.BytesIO(raw) as stream:
        source = bounded_io.VerifiedByteRange(stream, len(raw), None, chunk_size=4)
        assert source[selection] == raw[selection]


@pytest.mark.parametrize('needle', [b'', b'\n', b'two', b'e\nt', b'absent'])
@pytest.mark.parametrize('start,end', [(0, None), (2, 14), (-8, -1), (99, None)])
def test_verified_range_search_matches_bytes(needle, start, end):
    import bounded_io

    raw = b'one\ntwo\nthree\nfour'
    with io.BytesIO(raw) as stream:
        source = bounded_io.VerifiedByteRange(stream, len(raw), None, chunk_size=4)
        assert source.find(needle, start, end) == raw.find(needle, start, end)
        assert source.rfind(needle, start, end) == raw.rfind(needle, start, end)


def test_verified_range_checks_reloaded_bytes_and_final_content():
    import bounded_io

    with io.BytesIO(b'abcdefgh') as stream:
        source = bounded_io.VerifiedByteRange(stream, 8, None, chunk_size=4)
        assert source[:4] == b'abcd'
        position = stream.tell()
        stream.seek(4)
        stream.write(b'evil')
        stream.seek(position)
        with pytest.raises(ValueError, match='changed after verification'):
            source[4:]
        with pytest.raises(bounded_io.SourceChangedDuringRead):
            source.verify_content()


def test_verified_newline_index_is_immutable_and_utf8_is_checked():
    import bounded_io

    with io.BytesIO(b'a\nb\n\xff') as stream:
        source = bounded_io.VerifiedByteRange(stream, 5, None, chunk_size=2)
        offsets = source.newline_offsets()
        assert list(offsets) == [1, 3]
        assert offsets.readonly
        with pytest.raises(UnicodeDecodeError):
            source.newline_offsets(validate_utf8=True)


_ROOT = Path(__file__).resolve().parents[1]


def test_bounded_io_has_no_executor_dependencies():
    source = (_ROOT / 'scripts/bounded_io.py').read_text(encoding='utf-8')
    assert _nonstdlib_imports(source) <= {"llm_wiki.path_ancestry"}


@pytest.mark.parametrize('source', [
    'import compile_memory', 'from markdown_transaction import MarkdownCoordinator',
    'def read():\n import reliable_memory as runtime', 'from memory_state import *',
    'from . import local', 'from .os import read',
])
def test_boundary_rejects_executor_and_relative_imports(source):
    assert _nonstdlib_imports(source)


def test_boundary_accepts_standard_library():
    assert not _nonstdlib_imports('import os as system\nfrom pathlib import Path')


def test_complete_bounded_io_context_fits_user_ceiling():
    manifest = json.loads((_ROOT / 'docs/modules/bounded-io-context.json').read_text(encoding='utf-8'))
    paths = manifest['files']
    assert {'scripts/bounded_io.py', 'tests/test_bounded_io.py', 'tests/test_stable_source_ranges.py',
            'docs/modules/bounded-io.md', 'docs/modules/bounded-io-context.json'} <= set(paths)
    assert len(paths) == len(set(paths))
    assert manifest['max_tokens'] == 100_000
    from tests.module_context import measure_context
    total, files = measure_context(_ROOT, _ROOT / 'docs/modules/bounded-io-context.json')
    print(f'bounded-io: {total} tokens; {files} files')


def test_boundary_only_allows_the_exact_ancestry_owner():
    allowed = {"llm_wiki.path_ancestry"}
    assert _nonstdlib_imports('from llm_wiki.path_ancestry import acceptable_ancestor') <= allowed
    assert not _nonstdlib_imports('from llm_wiki import memory_queue') <= allowed
    assert not _nonstdlib_imports('from llm_wiki.runtime_publication import publish_runtime_file') <= allowed


@pytest.mark.parametrize('platform,birthtime,ctime,expected', [
    ('nt', 10, 20, True), ('nt', 11, 10, False),
    ('posix', 10, 20, False), ('posix', 11, 10, True),
])
def test_cross_api_timestamp_comparison_uses_matching_semantics(monkeypatch, platform, birthtime, ctime, expected):
    from types import SimpleNamespace

    import bounded_io

    before = SimpleNamespace(st_birthtime_ns=10, st_ctime_ns=10)
    opened = SimpleNamespace(st_birthtime_ns=birthtime, st_ctime_ns=ctime)
    monkeypatch.setattr(bounded_io, 'os', SimpleNamespace(name=platform))
    assert bounded_io.path_descriptor_change_time_matches(before, opened) is expected
