from __future__ import annotations

import ast
import io
import json
import os
import sys
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


def _import_roots(node):
    if isinstance(node, ast.Import):
        return {alias.name.split('.')[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return {('.' * node.level + (node.module or '')).split('.')[0]}
    return set()


def _nonstdlib_imports(source):
    roots = set().union(*(_import_roots(node) for node in ast.walk(ast.parse(source))))
    return roots - sys.stdlib_module_names


def test_bounded_io_has_no_executor_dependencies():
    source = (_ROOT / 'scripts/bounded_io.py').read_text(encoding='utf-8')
    assert not _nonstdlib_imports(source)


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
    manifest = json.loads((_ROOT / 'docs/modules/bounded-io-context.json').read_text())
    paths = manifest['files']
    assert {'scripts/bounded_io.py', 'tests/test_bounded_io.py', 'tests/test_stable_source_ranges.py',
            'docs/modules/bounded-io.md', 'docs/modules/bounded-io-context.json'} <= set(paths)
    assert len(paths) == len(set(paths))
    assert manifest['max_tokens'] == 100_000
    from tests.module_context import measure_context
    total, files = measure_context(_ROOT, _ROOT / 'docs/modules/bounded-io-context.json')
    print(f'bounded-io: {total} tokens; {files} files')
