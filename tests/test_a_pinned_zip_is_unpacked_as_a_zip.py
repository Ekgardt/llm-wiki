"""Audit 3, B18: the Windows Go toolchain is pinned as a zip and must unpack as one.

Research: `docs/research/2026-09-17-a-pinned-zip-is-unpacked-as-a-zip.md`.
"""

from __future__ import annotations

import io
import stat
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import install_language_server as installer  # noqa: E402

WINDOWS_GO_URL = "https://example.invalid/go1.27.1.windows-amd64.zip"


def _entry(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.external_attr = mode << 16
    return info


def _zip_of(entries: list[tuple[zipfile.ZipInfo, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for info, content in entries:
            archive.writestr(info, content)
    return buffer.getvalue()


def _unpack(content: bytes, root: Path, **bounds) -> None:
    installer._extract(
        content,
        root,
        installer._Placement(strip=1),
        mode=installer._archive_mode(WINDOWS_GO_URL),
        **bounds,
    )


def test_a_zip_lands_where_a_tarball_would(tmp_path: Path) -> None:
    content = _zip_of(
        [
            (_entry("go/bin/go.exe", stat.S_IFREG | 0o755), b"MZ"),
            (_entry("go/VERSION", 0), b"go1.27.1"),
        ]
    )
    _unpack(content, tmp_path)
    found = (
        (tmp_path / "bin/go.exe").read_bytes(),
        (tmp_path / "VERSION").read_bytes(),
        (tmp_path / "go").exists(),
    )
    assert found == (b"MZ", b"go1.27.1", False)


@pytest.mark.parametrize(
    "info",
    [
        _entry("go/../../escape", stat.S_IFREG | 0o644),
        _entry("/absolute", stat.S_IFREG | 0o644),
        _entry("go/link", stat.S_IFLNK | 0o777),
        # Escapes by the rules of a system that may not be the one unpacking:
        # the entry's own name decides, the same way everywhere.
        _entry("C:/drive", stat.S_IFREG | 0o644),
        _entry("C:relative-to-a-drive", stat.S_IFREG | 0o644),
        _entry("\\rooted", stat.S_IFREG | 0o644),
        _entry("go\\..\\..\\escape", stat.S_IFREG | 0o644),
    ],
)
def test_an_entry_that_escapes_or_is_not_a_file_is_refused(tmp_path: Path, info) -> None:
    with pytest.raises(installer.InstallError):
        _unpack(_zip_of([(info, b"x")]), tmp_path / "root")
    assert not (tmp_path / "escape").exists()


def test_the_three_bounds_hold_for_a_zip(tmp_path: Path) -> None:
    content = _zip_of(
        [
            (_entry("go/a", stat.S_IFREG | 0o644), b"12345"),
            (_entry("go/b", stat.S_IFREG | 0o644), b"12345"),
        ]
    )
    refused = []
    for bounds in ({"member_limit": 4}, {"limit": 9}, {"members_limit": 1}):
        with pytest.raises(installer.InstallError):
            _unpack(content, tmp_path / "root", **bounds)
        refused.append(sorted(bounds))
    assert refused == [["member_limit"], ["limit"], ["members_limit"]]
