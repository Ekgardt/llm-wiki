"""LSP coordinates, ranges and locations read from a server answer, each bounded before it is kept."""

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass

from llm_wiki.pyright_session_values import LspLocation
from lsp_positions import LspPosition, LspRange
from lsp_protocol import MAX_LOCATIONS

# LSP 3.17 base type `uinteger`: "an unsigned integer number in the range of 0 to 2^31 - 1".
_LSP_UINTEGER_MAX = 2**31 - 1
# Text fields of a server answer: a symbol name 4 KiB, a diagnostic message 64 KiB, a progress
# message 4 KiB. A longer field makes that item unreadable. Values predate measurement.
MAX_CALL_ITEM_TEXT_BYTES = 4096


def lsp_coordinate(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _LSP_UINTEGER_MAX
    ):
        return None
    return value


def _lsp_position(value: object) -> LspPosition | None:
    """One protocol position, when both coordinates are usable."""
    if not isinstance(value, dict):
        return None
    line = lsp_coordinate(value.get("line"))
    character = lsp_coordinate(value.get("character"))
    if line is None or character is None:
        return None
    return LspPosition(line, character)


def _ordered_lsp_range(
    start: LspPosition | None, end: LspPosition | None
) -> LspRange | None:
    """A range only exists when both ends read and the end is not before the start."""
    if start is None or end is None:
        return None
    if (end.line, end.character) < (start.line, start.character):
        return None
    return LspRange(start, end)


def lsp_range(value: object) -> LspRange | None:
    if not isinstance(value, dict):
        return None
    return _ordered_lsp_range(
        _lsp_position(value.get("start")), _lsp_position(value.get("end"))
    )


def location_key(location: LspLocation) -> tuple[object, ...]:
    return (
        location.uri,
        location.range.start.line,
        location.range.start.character,
        location.range.end.line,
        location.range.end.character,
    )


def range_json(value: LspRange) -> dict[str, dict[str, int]]:
    return {
        "start": {
            "line": value.start.line,
            "character": value.start.character,
        },
        "end": {
            "line": value.end.line,
            "character": value.end.character,
        },
    }


def bounded_text(value: object, max_bytes: int) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return None
    return value if size <= max_bytes else None


def symbol_tags_ok(tags: object) -> bool:
    if not isinstance(tags, list) or len(tags) > 32:
        return False
    return all(lsp_coordinate(tag) is not None for tag in tags)


def range_contains(outer: LspRange, inner: LspRange) -> bool:
    """The inner range starts no earlier and ends no later than the outer one."""
    starts_inside = (inner.start.line, inner.start.character) >= (
        outer.start.line,
        outer.start.character,
    )
    ends_inside = (inner.end.line, inner.end.character) <= (
        outer.end.line,
        outer.end.character,
    )
    return starts_inside and ends_inside


@dataclass
class BoundedLocations:
    """Distinct locations, up to the shared MAX_LOCATIONS bound."""

    locations: list[LspLocation] = dataclasses.field(default_factory=list)
    seen: set[tuple[object, ...]] = dataclasses.field(default_factory=set)

    def add(self, location: LspLocation) -> None:
        key = location_key(location)
        if key in self.seen or len(self.locations) >= MAX_LOCATIONS:
            return
        self.seen.add(key)
        self.locations.append(location)


def location_payload(result: object) -> list[object] | None:
    """The list of raw locations a provider returned, in either shape."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return [result]
    return None


def call_item_range_value(value: Mapping[str, object]) -> object:
    """A call item names its selection range, or falls back to its range."""
    if lsp_range(value.get("selectionRange")) is not None:
        return value.get("selectionRange")
    return value.get("range")


def location_fields(value: Mapping[str, object]) -> tuple[object, object] | None:
    """The URI and range of a location, in either of the protocol's two shapes."""
    if "targetUri" not in value and "targetSelectionRange" not in value:
        return value.get("uri"), value.get("range")
    if lsp_range(value.get("targetRange")) is None:
        return None
    return value.get("targetUri"), value.get("targetSelectionRange")
