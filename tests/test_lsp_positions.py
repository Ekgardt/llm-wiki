"""UTF position and file URI normalization tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from code_intelligence import PositionEncoding, PositionRange
from lsp_positions import (
    LspPosition,
    LspRange,
    SourceAnchor,
    SourceDocument,
    file_uri_to_path,
    path_to_file_uri,
)


def test_position_contracts_are_frozen_and_slotted() -> None:
    anchor = SourceAnchor("pkg/unicode_api.py", 1, 0)
    position = LspPosition(0, 0)
    value = LspRange(position, position)

    with pytest.raises(FrozenInstanceError):
        anchor.line = 2  # type: ignore[misc]
    assert not hasattr(anchor, "__dict__")
    assert not hasattr(position, "__dict__")
    assert not hasattr(value, "__dict__")


def test_utf8_anchor_converts_to_each_negotiated_encoding() -> None:
    document = SourceDocument.from_bytes("pkg/unicode_api.py", "a😀β\r\n".encode())
    anchor = document.validate_anchor(line=1, character=len("a😀".encode()))

    assert anchor == SourceAnchor("pkg/unicode_api.py", 1, 5)
    assert document.to_lsp(anchor, PositionEncoding.UTF8) == LspPosition(0, 5)
    assert document.to_lsp(anchor, PositionEncoding.UTF16) == LspPosition(0, 3)
    assert document.to_lsp(anchor, PositionEncoding.UTF32) == LspPosition(0, 2)


@pytest.mark.parametrize("content", [b"first\nsecond", b"first\r\nsecond", b"first\rsecond"])
def test_all_line_endings_map_to_absolute_half_open_byte_ranges(content: bytes) -> None:
    document = SourceDocument.from_bytes("example.py", content)
    value = LspRange(LspPosition(0, 5), LspPosition(1, 0))

    assert document.to_byte_range(value, PositionEncoding.UTF8) == PositionRange(
        5, content.index(b"second")
    )


@pytest.mark.parametrize(
    ("content", "line", "character", "absolute"),
    [
        (b"x", 1, 1, 1),
        (b"x\n", 2, 0, 2),
        (b"x\r\n", 2, 0, 3),
        (b"x\r", 2, 0, 2),
        (b"", 1, 0, 0),
    ],
)
def test_eol_and_trailing_empty_line_anchors_are_supported(
    content: bytes, line: int, character: int, absolute: int
) -> None:
    document = SourceDocument.from_bytes("example.py", content)
    anchor = document.validate_anchor(line=line, character=character)
    position = document.to_lsp(anchor, PositionEncoding.UTF8)

    assert document.to_byte_range(LspRange(position, position), PositionEncoding.UTF8) == (
        PositionRange(absolute, absolute)
    )


@pytest.mark.parametrize("value", [True, -1, 1.5])
def test_anchor_rejects_non_integer_and_negative_coordinates(value: object) -> None:
    document = SourceDocument.from_bytes("example.py", b"x")

    with pytest.raises((TypeError, ValueError)):
        document.validate_anchor(line=value, character=0)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        document.validate_anchor(line=1, character=value)  # type: ignore[arg-type]


def test_anchor_rejects_invalid_lines_offsets_codepoint_splits_and_newline_interior() -> None:
    document = SourceDocument.from_bytes("example.py", "a😀\r\nnext".encode())

    for line, character in ((0, 0), (3, 0), (1, 6), (1, 2)):
        with pytest.raises(ValueError):
            document.validate_anchor(line=line, character=character)


def test_document_rejects_invalid_utf8() -> None:
    with pytest.raises(UnicodeDecodeError):
        SourceDocument.from_bytes("example.py", b"\xff")


def test_document_rejects_wrong_path_content_and_encoding_types() -> None:
    with pytest.raises((TypeError, ValueError)):
        SourceDocument.from_bytes("bad\0.py", b"x")
    with pytest.raises(TypeError):
        SourceDocument.from_bytes("example.py", "x")  # type: ignore[arg-type]

    document = SourceDocument.from_bytes("example.py", b"x")
    with pytest.raises(TypeError):
        document.to_lsp(SourceAnchor("other.py", 1, 0), PositionEncoding.UTF8)
    with pytest.raises(TypeError):
        document.to_lsp(SourceAnchor("example.py", 1, 0), "utf-8")  # type: ignore[arg-type]


def test_lsp_range_rejects_utf16_surrogate_midpoint_and_reversed_range() -> None:
    document = SourceDocument.from_bytes("example.py", "a😀β\nnext".encode())

    with pytest.raises(ValueError):
        document.to_byte_range(
            LspRange(LspPosition(0, 2), LspPosition(0, 3)), PositionEncoding.UTF16
        )
    with pytest.raises(ValueError):
        document.to_byte_range(
            LspRange(LspPosition(1, 0), LspPosition(0, 0)), PositionEncoding.UTF8
        )


@pytest.mark.parametrize("line,character", [(-1, 0), (0, -1), (True, 0), (0, True)])
def test_lsp_position_rejects_invalid_coordinates(line: object, character: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        LspPosition(line, character)  # type: ignore[arg-type]


def test_lsp_range_rejects_out_of_range_positions() -> None:
    document = SourceDocument.from_bytes("example.py", b"x")

    with pytest.raises(ValueError):
        document.to_byte_range(
            LspRange(LspPosition(0, 0), LspPosition(0, 2)), PositionEncoding.UTF8
        )


def test_file_uri_round_trips_windows_drive_case_space_and_unicode() -> None:
    path = PureWindowsPath("c:/repo name/pkg/β.py")
    uri = path_to_file_uri(path)

    assert uri == "file:///C:/repo%20name/pkg/%CE%B2.py"
    assert file_uri_to_path(uri) == Path("C:/repo name/pkg/β.py")


def test_file_uri_round_trips_posix_absolute_path() -> None:
    path = PurePosixPath("/repo name/pkg/β.py")
    uri = path_to_file_uri(path)

    assert uri == "file:///repo%20name/pkg/%CE%B2.py"
    assert file_uri_to_path(uri).as_posix() == "/repo name/pkg/β.py"


def test_file_uri_round_trips_unc_path() -> None:
    path = PureWindowsPath(r"\\server\share name\pkg\api.py")
    uri = path_to_file_uri(path)

    assert uri == "file://server/share%20name/pkg/api.py"
    assert file_uri_to_path(uri) == Path(r"\\server\share name\pkg\api.py")


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.test/file.py",
        "file:///tmp/bad%2",
        "file:///tmp/bad%GG",
        "file:///tmp/bad%00.py",
        "file:///tmp/bad\0.py",
        "file:///tmp/file.py?query",
        "file:///tmp/file.py#fragment",
    ],
)
def test_file_uri_rejects_invalid_inputs(uri: str) -> None:
    with pytest.raises(ValueError):
        file_uri_to_path(uri)


def test_file_uri_conversion_does_not_enforce_containment() -> None:
    assert file_uri_to_path("file:///repo/%2E%2E/outside.py").as_posix() == (
        "/repo/../outside.py"
    )


@pytest.mark.parametrize(
    "path", [PurePosixPath("relative.py"), PureWindowsPath("relative.py"), Path("bad\0.py")]
)
def test_path_to_file_uri_rejects_relative_and_nul_paths(path: Path) -> None:
    with pytest.raises(ValueError):
        path_to_file_uri(path)


def test_unicode_fixture_contains_position_boundary_sample() -> None:
    fixture = Path(__file__).parent / "fixtures/code_kernel/python/pkg/unicode_api.py"

    assert "a😀β" in fixture.read_text(encoding="utf-8")
