"""Strict conversion between source bytes, LSP positions, and file URIs."""

from __future__ import annotations

import hashlib
import os
import re
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from threading import Lock
from urllib.parse import quote, unquote_to_bytes, urlsplit

from code_intelligence import PositionEncoding, PositionRange

_MALFORMED_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ENCODED_PATH_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:$")
_BOUNDARY_CHECKPOINT_STRIDE = 256
_BOUNDARY_INDEX_CACHE_LINES = 128


def _require_coordinate(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


def _require_path(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("path must be a string")
    if not value:
        raise ValueError("path must not be empty")
    if "\0" in value:
        raise ValueError("path must not contain NUL")
    return value


def _require_encoding(value: object) -> PositionEncoding:
    if not isinstance(value, PositionEncoding):
        raise TypeError("encoding must be PositionEncoding")
    return value


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    path: str
    line: int
    utf8_character: int
    byte_offset: int

    def __post_init__(self) -> None:
        _require_path(self.path)
        _require_coordinate(self.line, "line", minimum=1)
        _require_coordinate(self.utf8_character, "utf8_character")
        _require_coordinate(self.byte_offset, "byte_offset")


@dataclass(frozen=True, slots=True)
class LspPosition:
    line: int
    character: int

    def __post_init__(self) -> None:
        _require_coordinate(self.line, "line")
        _require_coordinate(self.character, "character")


@dataclass(frozen=True, slots=True)
class LspRange:
    start: LspPosition
    end: LspPosition

    def __post_init__(self) -> None:
        if not isinstance(self.start, LspPosition) or not isinstance(self.end, LspPosition):
            raise TypeError("range endpoints must be LspPosition")


@dataclass(frozen=True, slots=True)
class _LineBoundaryIndex:
    byte_offsets: tuple[int, ...]
    utf16_offsets: tuple[int, ...]
    utf32_offsets: tuple[int, ...]


_LINE_BOUNDARY_INDEXES: OrderedDict[tuple[str, int], _LineBoundaryIndex] = OrderedDict()
_LINE_BOUNDARY_CACHE_LOCK = Lock()


def _build_line_boundary_index(
    content: bytes, start: int, end: int
) -> _LineBoundaryIndex:
    byte_offsets = [0]
    utf16_offsets = [0]
    utf32_offsets = [0]
    byte_offset = 0
    utf16_offset = 0
    utf32_offset = 0
    while start + byte_offset < end:
        width = _utf8_code_point_width(content[start + byte_offset])
        byte_offset += width
        utf16_offset += 2 if width == 4 else 1
        utf32_offset += 1
        if utf32_offset % _BOUNDARY_CHECKPOINT_STRIDE == 0:
            byte_offsets.append(byte_offset)
            utf16_offsets.append(utf16_offset)
            utf32_offsets.append(utf32_offset)
    if byte_offsets[-1] != byte_offset:
        byte_offsets.append(byte_offset)
        utf16_offsets.append(utf16_offset)
        utf32_offsets.append(utf32_offset)
    return _LineBoundaryIndex(
        tuple(byte_offsets), tuple(utf16_offsets), tuple(utf32_offsets)
    )


def _line_boundary_index(
    source_sha256: str,
    line_number: int,
    content: bytes,
    start: int,
    end: int,
) -> _LineBoundaryIndex:
    key = (source_sha256, line_number)
    with _LINE_BOUNDARY_CACHE_LOCK:
        index = _LINE_BOUNDARY_INDEXES.get(key)
        if index is not None:
            _LINE_BOUNDARY_INDEXES.move_to_end(key)
            return index
        index = _build_line_boundary_index(content, start, end)
        _LINE_BOUNDARY_INDEXES[key] = index
        if len(_LINE_BOUNDARY_INDEXES) > _BOUNDARY_INDEX_CACHE_LINES:
            _LINE_BOUNDARY_INDEXES.popitem(last=False)
        return index


def _clear_line_boundary_cache() -> None:
    with _LINE_BOUNDARY_CACHE_LOCK:
        _LINE_BOUNDARY_INDEXES.clear()


def _utf8_code_point_width(first_byte: int) -> int:
    if first_byte < 0x80:
        return 1
    if first_byte < 0xE0:
        return 2
    if first_byte < 0xF0:
        return 3
    return 4


@dataclass(frozen=True, slots=True)
class SourceDocument:
    path: str
    content: bytes
    source_sha256: str
    line_spans: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        _require_path(self.path)
        if not isinstance(self.content, bytes):
            raise TypeError("content must be bytes")
        self.content.decode("utf-8", errors="strict")
        if self.source_sha256 != hashlib.sha256(self.content).hexdigest():
            raise ValueError("source_sha256 does not match content")
        if self.line_spans != self._scan_line_spans(self.content):
            raise ValueError("line_spans do not match content")

    @classmethod
    def from_bytes(cls, path: str, content: bytes) -> SourceDocument:
        _require_path(path)
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        content.decode("utf-8", errors="strict")
        return cls(
            path,
            content,
            hashlib.sha256(content).hexdigest(),
            cls._scan_line_spans(content),
        )

    @staticmethod
    def _scan_line_spans(content: bytes) -> tuple[tuple[int, int], ...]:
        spans: list[tuple[int, int]] = []
        start = 0
        index = 0
        while index < len(content):
            if content[index] not in (10, 13):
                index += 1
                continue
            spans.append((start, index))
            if content[index : index + 2] == b"\r\n":
                index += 2
            else:
                index += 1
            start = index
        spans.append((start, len(content)))
        return tuple(spans)

    def validate_anchor(self, *, line: int, character: int) -> SourceAnchor:
        _require_coordinate(line, "line", minimum=1)
        _require_coordinate(character, "character")
        if line > len(self.line_spans):
            raise ValueError("line is outside the document")
        start, end = self.line_spans[line - 1]
        if character > end - start:
            raise ValueError("character is outside the line")
        self.content[start : start + character].decode("utf-8", errors="strict")
        return SourceAnchor(self.path, line, character, start + character)

    def to_lsp(self, anchor: SourceAnchor, encoding: PositionEncoding) -> LspPosition:
        if not isinstance(anchor, SourceAnchor):
            raise TypeError("anchor must be SourceAnchor")
        _require_encoding(encoding)
        if anchor.path != self.path:
            raise TypeError("anchor belongs to a different document")
        validated = self.validate_anchor(
            line=anchor.line, character=anchor.utf8_character
        )
        if anchor != validated:
            raise ValueError("anchor byte_offset does not match the document")
        start, _ = self.line_spans[validated.line - 1]
        prefix = self.content[start : validated.byte_offset].decode("utf-8")
        if encoding is PositionEncoding.UTF8:
            character = validated.utf8_character
        elif encoding is PositionEncoding.UTF16:
            character = len(prefix.encode("utf-16-le")) // 2
        else:
            character = len(prefix)
        return LspPosition(validated.line - 1, character)

    def to_byte_range(self, value: LspRange, encoding: PositionEncoding) -> PositionRange:
        if not isinstance(value, LspRange):
            raise TypeError("value must be LspRange")
        _require_encoding(encoding)
        start = self._lsp_to_byte_offset(value.start, encoding)
        end = self._lsp_to_byte_offset(value.end, encoding)
        if end < start:
            raise ValueError("range end must not precede range start")
        return PositionRange(start, end)

    def _lsp_to_byte_offset(
        self, position: LspPosition, encoding: PositionEncoding
    ) -> int:
        if position.line >= len(self.line_spans):
            raise ValueError("line is outside the document")
        start, end = self.line_spans[position.line]
        line_length = end - start
        index = _line_boundary_index(
            self.source_sha256, position.line, self.content, start, end
        )

        if encoding is PositionEncoding.UTF8:
            offsets = index.byte_offsets
        elif encoding is PositionEncoding.UTF16:
            offsets = index.utf16_offsets
        else:
            offsets = index.utf32_offsets
        checkpoint = bisect_right(offsets, position.character) - 1
        byte_offset = index.byte_offsets[checkpoint]
        utf16_offset = index.utf16_offsets[checkpoint]
        utf32_offset = index.utf32_offsets[checkpoint]

        while byte_offset < line_length:
            if encoding is PositionEncoding.UTF8:
                units = byte_offset
            elif encoding is PositionEncoding.UTF16:
                units = utf16_offset
            else:
                units = utf32_offset
            if units >= position.character:
                break
            width = _utf8_code_point_width(self.content[start + byte_offset])
            byte_offset += width
            utf16_offset += 2 if width == 4 else 1
            utf32_offset += 1

        if encoding is PositionEncoding.UTF8:
            units = byte_offset
        elif encoding is PositionEncoding.UTF16:
            units = utf16_offset
        else:
            units = utf32_offset
        if units != position.character:
            raise ValueError("character is not a valid code-unit boundary")
        return start + byte_offset

def path_to_file_uri(path: PurePath) -> str:
    """Convert an absolute POSIX, drive, or UNC path to a normalized file URI."""
    if not isinstance(path, (PurePosixPath, PureWindowsPath)):
        raise TypeError("path must be a pathlib path")
    raw = str(path)
    if "\0" in raw:
        raise ValueError("path must not contain NUL")
    if not path.is_absolute():
        raise ValueError("path must be absolute")

    if isinstance(path, PureWindowsPath):
        if raw.startswith(("\\\\.\\", "\\\\?\\")):
            raise ValueError("Windows device namespaces are not supported")
        if path.drive.startswith("\\\\"):
            server, share = path.drive[2:].split("\\", 1)
            tail = "/".join(path.parts[1:])
            uri_path = "/" + quote(share + ("/" + tail if tail else ""), safe="/")
            return f"file://{quote(server, safe='-._~[]:')}{uri_path}"
        if not _WINDOWS_DRIVE.fullmatch(path.drive):
            raise ValueError("Windows path must use a drive letter or UNC share")
        normalized = path.as_posix()
        normalized = path.drive[0].upper() + normalized[1:]
        return "file:///" + quote(normalized, safe="/:")

    return "file://" + quote(path.as_posix(), safe="/")


def file_uri_to_path(uri: str, *, platform: str | None = None) -> PurePath:
    """Convert a validated file URI to a local path without containment checks."""
    if not isinstance(uri, str):
        raise TypeError("uri must be a string")
    if not uri or any(ord(character) < 32 for character in uri):
        raise ValueError("uri must not contain control characters")
    if "\\" in uri:
        raise ValueError("file uri must not contain raw backslashes")
    if _MALFORMED_PERCENT.search(uri):
        raise ValueError("uri contains malformed percent encoding")
    target_platform = os.name if platform is None else platform
    if target_platform not in {"nt", "posix"}:
        raise ValueError("platform must be 'nt' or 'posix'")

    parsed = urlsplit(uri)
    if parsed.scheme.lower() != "file":
        raise ValueError("uri must use the file scheme")
    if parsed.query or parsed.fragment:
        raise ValueError("file uri must not contain a query or fragment")
    if _ENCODED_PATH_SEPARATOR.search(parsed.path):
        raise ValueError("file uri path must not contain encoded separators")

    try:
        authority = unquote_to_bytes(parsed.netloc).decode("utf-8", errors="strict")
        decoded_path = unquote_to_bytes(parsed.path).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("file uri must contain valid UTF-8") from exc
    if any(ord(character) < 32 or ord(character) == 127 for character in authority):
        raise ValueError("file uri authority must not contain control characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded_path):
        raise ValueError("file uri path must not contain control characters")
    if "/" in authority or "\\" in authority:
        raise ValueError("file uri authority must not contain separators")
    if "\\" in decoded_path:
        raise ValueError("file uri path must not contain backslashes")
    if "@" in authority:
        raise ValueError("file uri authority must not contain userinfo")
    if authority in {".", "?"}:
        raise ValueError("Windows device authorities are not supported")
    if authority.startswith("["):
        if re.fullmatch(r"\[[^]]+\]", authority) is None:
            raise ValueError("file uri authority must not contain a port")
    elif ":" in authority:
        raise ValueError("file uri authority must not contain a port")

    if authority and authority.lower() != "localhost":
        if not decoded_path.startswith("/"):
            raise ValueError("UNC file uri path must be absolute")
        if target_platform == "nt":
            return PureWindowsPath(
                "\\\\" + authority + decoded_path.replace("/", "\\")
            )
        return PurePosixPath("//" + authority + decoded_path)

    if not decoded_path.startswith("/"):
        if target_platform != "nt" or re.match(r"^[A-Za-z]:/", decoded_path) is None:
            raise ValueError("file uri path must be absolute")
    if target_platform == "nt":
        drive_match = re.match(r"^/?([A-Za-z]):/(.*)$", decoded_path)
        if drive_match:
            normalized = drive_match.group(1).upper() + ":/" + drive_match.group(2)
            return PureWindowsPath(normalized)
        raise ValueError("Windows file uri must include a drive or UNC authority")
    return PurePosixPath(decoded_path)
