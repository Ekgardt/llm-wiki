"""Existing Pyright metadata JSON/JSONC dialect; parsing values without IO."""
from __future__ import annotations

import json


class MetadataError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_json_number(_value: str) -> object:
    raise ValueError("non-integral JSON number")


def strict_json_object(
    raw: bytes,
    malformed_code: str,
    *,
    recursion_code: str | None = None,
) -> dict[str, object]:
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_float=reject_json_number,
            parse_constant=reject_json_number,
        )
    except RecursionError as exc:
        raise MetadataError(recursion_code or malformed_code) from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MetadataError(malformed_code) from exc
    if not isinstance(value, dict):
        raise MetadataError(malformed_code)
    return value


def blank_unless_newline(character: str) -> str:
    if character in "\r\n":
        return character
    return " "


class CommentStripper:
    """Replace JSONC comments with spaces (newlines kept) outside strings."""

    def __init__(self, text: str, malformed_code: str) -> None:
        self.text = text
        self.code = malformed_code
        self.out: list[str] = []
        self.index = 0
        self.in_string = False
        self.escaped = False

    def run(self) -> list[str]:
        while self.index < len(self.text):
            self._step()
        return self.out

    def _keep(self, character: str) -> None:
        self.out.append(character)
        self.index += 1

    def _advance_string_state(self, character: str) -> None:
        if self.escaped:
            self.escaped = False
            return
        self._unescaped_string_character(character)

    def _unescaped_string_character(self, character: str) -> None:
        if character == "\\":
            self.escaped = True
            return
        if character == '"':
            self.in_string = False

    def _step(self) -> None:
        character = self.text[self.index]
        if self.in_string:
            self._advance_string_state(character)
            self._keep(character)
            return
        self._step_outside_string(character)

    def _step_outside_string(self, character: str) -> None:
        if character == '"':
            self.in_string = True
            self._keep(character)
            return
        if character != "/" or self.index + 1 >= len(self.text):
            self._keep(character)
            return
        self._comment_or_slash(self.text[self.index + 1], character)

    def _comment_or_slash(self, marker: str, character: str) -> None:
        if marker == "/":
            self._skip_line_comment()
            return
        if marker != "*":
            self._keep(character)
            return
        self._skip_block_comment()

    def _skip_line_comment(self) -> None:
        self.out.extend((" ", " "))
        self.index += 2
        while self.index < len(self.text) and self.text[self.index] not in "\r\n":
            self.out.append(" ")
            self.index += 1

    def _at_block_end(self) -> bool:
        return self.text[self.index] == "*" and self.text[self.index + 1 : self.index + 2] == "/"

    def _skip_block_comment(self) -> None:
        self.out.extend((" ", " "))
        self.index += 2
        while self.index < len(self.text):
            if self._at_block_end():
                self.out.extend((" ", " "))
                self.index += 2
                return
            self.out.append(blank_unless_newline(self.text[self.index]))
            self.index += 1
        raise MetadataError(self.code)


class TrailingCommaPass:
    """Blank a comma that directly precedes `}` or `]` outside strings."""

    def __init__(self, normalized: list[str]) -> None:
        self.normalized = normalized
        self.pending_comma: int | None = None
        self.previous: str | None = None
        self.in_string = False
        self.escaped = False

    def run(self) -> list[str]:
        for index, character in enumerate(self.normalized):
            self._step(index, character)
        return self.normalized

    def _string_character(self, character: str) -> None:
        if self.escaped:
            self.escaped = False
            return
        self._unescaped_string_character(character)

    def _unescaped_string_character(self, character: str) -> None:
        if character == "\\":
            self.escaped = True
            return
        if character == '"':
            self.in_string = False
            self.previous = character

    def _comma(self, index: int) -> None:
        if self.previous in {None, "{", "[", ",", ":"}:
            self.pending_comma = None
        else:
            self.pending_comma = index
        self.previous = ","

    def _significant(self, character: str) -> None:
        if character in "}]" and self.pending_comma is not None:
            self.normalized[self.pending_comma] = " "
        self.pending_comma = None
        self.previous = character

    def _step(self, index: int, character: str) -> None:
        if self.in_string:
            self._string_character(character)
            return
        if character == '"':
            self.pending_comma = None
            self.previous = character
            self.in_string = True
            return
        self._step_outside_string(index, character)

    def _step_outside_string(self, index: int, character: str) -> None:
        if character == ",":
            self._comma(index)
            return
        if character not in " \t\r\n":
            self._significant(character)


def normalize_jsonc(raw: bytes, malformed_code: str) -> bytes:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise MetadataError(malformed_code) from exc
    normalized = CommentStripper(text, malformed_code).run()
    return "".join(TrailingCommaPass(normalized).run()).encode("utf-8")


# Existing Python serialization paths remain available through direct aliases.
CommentStripper.__module__ = "pyright_profile"
MetadataError.__module__ = "pyright_profile"
TrailingCommaPass.__module__ = "pyright_profile"
blank_unless_newline.__module__ = "pyright_profile"
normalize_jsonc.__module__ = "pyright_profile"
reject_duplicate_keys.__module__ = "pyright_profile"
reject_json_number.__module__ = "pyright_profile"
strict_json_object.__module__ = "pyright_profile"
