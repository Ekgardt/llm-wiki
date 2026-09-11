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
_ENCODED_FORWARD_SLASH = re.compile(r"%2f", re.IGNORECASE)
_ENCODED_BACKSLASH = re.compile(r"%5c", re.IGNORECASE)
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
        document = object.__new__(cls)
        object.__setattr__(document, "path", path)
        object.__setattr__(document, "content", content)
        object.__setattr__(document, "source_sha256", hashlib.sha256(content).hexdigest())
        object.__setattr__(document, "line_spans", cls._scan_line_spans(content))
        return document

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
        return LspPosition(
            validated.line - 1, _prefix_units(prefix, validated.utf8_character, encoding)
        )

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
        index = _line_boundary_index(
            self.source_sha256, position.line, self.content, start, end
        )
        checkpoint = bisect_right(_encoded_offsets(index, encoding), position.character) - 1
        triple = (
            index.byte_offsets[checkpoint],
            index.utf16_offsets[checkpoint],
            index.utf32_offsets[checkpoint],
        )
        triple = _walk_to_character(
            self.content, start, end - start, triple, position.character, encoding
        )
        if _units_of(triple, encoding) != position.character:
            raise ValueError("character is not a valid code-unit boundary")
        return start + triple[0]


def _prefix_units(prefix: str, utf8_character: int, encoding: PositionEncoding) -> int:
    """The length of a line prefix in the negotiated encoding's code units."""
    if encoding is PositionEncoding.UTF8:
        return utf8_character
    if encoding is PositionEncoding.UTF16:
        return len(prefix.encode("utf-16-le")) // 2
    return len(prefix)


def _encoded_offsets(index: _LineBoundaryIndex, encoding: PositionEncoding) -> list[int]:
    if encoding is PositionEncoding.UTF8:
        return index.byte_offsets
    if encoding is PositionEncoding.UTF16:
        return index.utf16_offsets
    return index.utf32_offsets


def _units_of(triple: tuple[int, int, int], encoding: PositionEncoding) -> int:
    """One boundary as (byte, utf16, utf32) offsets; the one the encoding counts."""
    byte_offset, utf16_offset, utf32_offset = triple
    if encoding is PositionEncoding.UTF8:
        return byte_offset
    if encoding is PositionEncoding.UTF16:
        return utf16_offset
    return utf32_offset


def _next_boundary(content: bytes, start: int, triple: tuple[int, int, int]) -> tuple[int, int, int]:
    byte_offset, utf16_offset, utf32_offset = triple
    width = _utf8_code_point_width(content[start + byte_offset])
    utf16_units = 2 if width == 4 else 1
    return (byte_offset + width, utf16_offset + utf16_units, utf32_offset + 1)


def _walk_to_character(
    content: bytes,
    start: int,
    line_length: int,
    triple: tuple[int, int, int],
    character: int,
    encoding: PositionEncoding,
) -> tuple[int, int, int]:
    """Advance code point by code point until the encoding's units reach `character`."""
    while triple[0] < line_length and _units_of(triple, encoding) < character:
        triple = _next_boundary(content, start, triple)
    return triple


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
        return _windows_file_uri(path, raw)
    return "file://" + quote(path.as_posix(), safe="/")


def _windows_file_uri(path: PureWindowsPath, raw: str) -> str:
    if raw.startswith(("\\\\.\\", "\\\\?\\")):
        raise ValueError("Windows device namespaces are not supported")
    if path.drive.startswith("\\\\"):
        return _unc_file_uri(path)
    if not _WINDOWS_DRIVE.fullmatch(path.drive):
        raise ValueError("Windows path must use a drive letter or UNC share")
    normalized = path.as_posix()
    normalized = path.drive[0].upper() + normalized[1:]
    return "file:///" + quote(normalized, safe="/:")


def _unc_file_uri(path: PureWindowsPath) -> str:
    server, share = path.drive[2:].split("\\", 1)
    tail = "/".join(path.parts[1:])
    share_path = share + ("/" + tail if tail else "")
    uri_path = "/" + quote(share_path, safe="/")
    return f"file://{quote(server, safe='-._~[]:')}{uri_path}"


def _has_control(text: str) -> bool:
    return any(ord(character) < 32 for character in text)


def _has_control_or_delete(text: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in text)


def _require_uri_string(uri: object) -> str:
    if not isinstance(uri, str):
        raise TypeError("uri must be a string")
    if not uri or _has_control(uri):
        raise ValueError("uri must not contain control characters")
    return uri


def _require_uri_syntax(uri: str) -> None:
    if "\\" in uri:
        raise ValueError("file uri must not contain raw backslashes")
    if _MALFORMED_PERCENT.search(uri):
        raise ValueError("uri contains malformed percent encoding")


def _target_platform(platform: str | None) -> str:
    target_platform = os.name if platform is None else platform
    if target_platform not in {"nt", "posix"}:
        raise ValueError("platform must be 'nt' or 'posix'")
    return target_platform


def _has_encoded_separator(path: str, target_platform: str) -> bool:
    if _ENCODED_FORWARD_SLASH.search(path):
        return True
    return target_platform == "nt" and bool(_ENCODED_BACKSLASH.search(path))


def _parsed_file_uri(uri: str, target_platform: str):
    parsed = urlsplit(uri)
    if parsed.scheme.lower() != "file":
        raise ValueError("uri must use the file scheme")
    if parsed.query or parsed.fragment:
        raise ValueError("file uri must not contain a query or fragment")
    if _has_encoded_separator(parsed.path, target_platform):
        raise ValueError("file uri path must not contain encoded separators")
    return parsed


def _decoded_uri_parts(parsed) -> tuple[str, str]:
    """(authority, path) percent-decoded as strict UTF-8, free of control characters."""
    try:
        authority = unquote_to_bytes(parsed.netloc).decode("utf-8", errors="strict")
        decoded_path = unquote_to_bytes(parsed.path).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("file uri must contain valid UTF-8") from exc
    if _has_control_or_delete(authority):
        raise ValueError("file uri authority must not contain control characters")
    if _has_control_or_delete(decoded_path):
        raise ValueError("file uri path must not contain control characters")
    return authority, decoded_path


def _require_no_port(authority: str) -> None:
    if authority.startswith("["):
        if re.fullmatch(r"\[[^]]+\]", authority) is None:
            raise ValueError("file uri authority must not contain a port")
        return
    if ":" in authority:
        raise ValueError("file uri authority must not contain a port")


def _require_plain_authority(authority: str) -> None:
    """Empty, `localhost`, or one host name: no userinfo, device name, or port."""
    if "@" in authority:
        raise ValueError("file uri authority must not contain userinfo")
    if authority in {".", "?"}:
        raise ValueError("Windows device authorities are not supported")
    _require_no_port(authority)


def _require_separator_free(authority: str, decoded_path: str, target_platform: str) -> None:
    if "/" in authority or "\\" in authority:
        raise ValueError("file uri authority must not contain separators")
    if target_platform == "nt" and "\\" in decoded_path:
        raise ValueError("file uri path must not contain backslashes")


def _is_unc_authority(authority: str) -> bool:
    return bool(authority) and authority.lower() != "localhost"


def _unc_path(authority: str, decoded_path: str, target_platform: str) -> PurePath:
    if not decoded_path.startswith("/"):
        raise ValueError("UNC file uri path must be absolute")
    if target_platform == "nt":
        return PureWindowsPath("\\\\" + authority + decoded_path.replace("/", "\\"))
    return PurePosixPath("//" + authority + decoded_path)


def _windows_drive_path(decoded_path: str) -> PureWindowsPath:
    drive_match = re.match(r"^/?([A-Za-z]):/(.*)$", decoded_path)
    if drive_match:
        return PureWindowsPath(drive_match.group(1).upper() + ":/" + drive_match.group(2))
    raise ValueError("Windows file uri must include a drive or UNC authority")


def _local_path(decoded_path: str, target_platform: str) -> PurePath:
    if not decoded_path.startswith("/"):
        if target_platform != "nt" or re.match(r"^[A-Za-z]:/", decoded_path) is None:
            raise ValueError("file uri path must be absolute")
    if target_platform == "nt":
        return _windows_drive_path(decoded_path)
    return PurePosixPath(decoded_path)


def file_uri_to_path(uri: str, *, platform: str | None = None) -> PurePath:
    """Convert a validated file URI to a local path without containment checks.

    A pipeline of named checks in a fixed order (RFC 8089 plus this
    repository's refusals); the order decides which error a bad input
    reports. See docs/research/2026-09-11-the-uri-parser-is-a-pipeline-of-named-checks.md.
    """
    _require_uri_syntax(_require_uri_string(uri))
    target_platform = _target_platform(platform)
    parsed = _parsed_file_uri(uri, target_platform)
    authority, decoded_path = _decoded_uri_parts(parsed)
    _require_separator_free(authority, decoded_path, target_platform)
    _require_plain_authority(authority)
    if _is_unc_authority(authority):
        return _unc_path(authority, decoded_path, target_platform)
    return _local_path(decoded_path, target_platform)
