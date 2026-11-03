"""Existing Unicode-control and terminal-sequence normalization for LSP text."""
from __future__ import annotations

import unicodedata


def _is_control(character: str) -> bool:
    codepoint = ord(character)
    return (
        codepoint < 32
        or 127 <= codepoint <= 159
        or unicodedata.category(character) in {"Cf", "Zl", "Zp"}
    )


def _collapses_to_space(character: str) -> bool:
    if ord(character) < 32 or 127 <= ord(character) <= 159:
        return True
    return unicodedata.category(character) in {"Zl", "Zp"}


class _LogScanner:
    """Drop terminal control sequences while keeping every printable character."""

    _STRING_INTRODUCERS = {
        "P": False,
        "X": False,
        "]": True,
        "^": False,
        "_": False,
    }
    _C1_STRING_INTRODUCERS = {
        "\x90": False,
        "\x98": False,
        "\x9d": True,
        "\x9e": False,
        "\x9f": False,
    }
    _C1_INTRODUCERS = frozenset(("\x9b", *_C1_STRING_INTRODUCERS))

    def __init__(self, value: str) -> None:
        self.value = value
        self.pieces: list[str] = []
        self.index = 0
        self.state = "ground"
        self.osc = False

    def run(self) -> str:
        while self.index < len(self.value):
            self._step()
        return "".join(self.pieces)

    def _step(self) -> None:
        character = self.value[self.index]
        if self._consumed_by_sequence(character):
            return
        self._emit(character)
        self.index += 1

    def _consumed_by_sequence(self, character: str) -> bool:
        """The first handler, in order, that takes the character; the rest never run."""
        handlers = (
            self._string_terminated,
            self._interrupted_by_introducer,
            self._inside_sequence,
            self._introduces_sequence,
        )
        return any(handler(character) for handler in handlers)

    def _escape_terminator_follows(self, character: str) -> bool:
        return character == "\x1b" and self.value[self.index + 1 : self.index + 2] == "\\"

    def _string_terminator_length(self, character: str) -> int:
        if character == "\x9c":
            return 1
        if self._escape_terminator_follows(character):
            return 2
        return self._bell_terminator_length(character)

    def _bell_terminator_length(self, character: str) -> int:
        if self.osc and character == "\x07":
            return 1
        return 0

    def _string_terminated(self, character: str) -> bool:
        if self.state != "string":
            return False
        length = self._string_terminator_length(character)
        if not length:
            return False
        self.state = "ground"
        self.index += length
        return True

    def _interrupted_by_introducer(self, character: str) -> bool:
        """A new introducer inside a sequence abandons the unfinished one."""
        if self.state == "ground":
            return False
        if character != "\x1b" and character not in self._C1_INTRODUCERS:
            return False
        self.state = "ground"
        return True

    @staticmethod
    def _escape_next_state(character: str) -> str:
        if "0" <= character <= "~":
            return "ground"
        if not " " <= character <= "/":
            return "ground"
        return "escape"

    def _inside_sequence(self, character: str) -> bool:
        if self.state == "ground":
            return False
        self.index += 1
        self.state = self._state_after(character)
        return True

    def _state_after(self, character: str) -> str:
        """The state after one character inside an escape, CSI or string sequence."""
        if self.state == "escape":
            return self._escape_next_state(character)
        if self.state == "csi" and "@" <= character <= "~":
            return "ground"
        return self.state

    def _skip_known_escape(self, following: str) -> None:
        if " " <= following <= "/":
            self.state = "escape"
            self.index += 2
            return
        if "0" <= following <= "~":
            self.index += 2
            return
        self.index += 1

    def _skip_escape_body(self, following: str) -> None:
        if following == "\\":
            self.index += 2
            return
        if not following:
            self.index += 1
            return
        self._skip_known_escape(following)

    def _escape_introducer(self) -> None:
        following = self.value[self.index + 1 : self.index + 2]
        if following == "[":
            self.state = "csi"
            self.index += 2
            return
        if following in self._STRING_INTRODUCERS:
            self.state = "string"
            self.osc = self._STRING_INTRODUCERS[following]
            self.index += 2
            return
        self._skip_escape_body(following)

    def _introduces_sequence(self, character: str) -> bool:
        if character == "\x1b":
            self._escape_introducer()
            return True
        return self._introduces_c1_sequence(character)

    def _introduces_c1_sequence(self, character: str) -> bool:
        if character == "\x9b":
            self.state = "csi"
            self.index += 1
            return True
        if character in self._C1_STRING_INTRODUCERS:
            self.state = "string"
            self.osc = self._C1_STRING_INTRODUCERS[character]
            self.index += 1
            return True
        return False

    def _append_safe_space(self) -> None:
        if not self.pieces or self.pieces[-1] != " ":
            self.pieces.append(" ")

    def _emit(self, character: str) -> None:
        if not _is_control(character):
            self.pieces.append(character)
            return
        if _collapses_to_space(character):
            self._append_safe_space()


def _normalize_log_text(value: str) -> str:
    return _LogScanner(value).run()
