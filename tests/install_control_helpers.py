"""Helpers shared by test_install_control.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path


def _release() -> dict[str, object]:
    return {
        "commit_oid": "a" * 40,
        "project_version": "4.0.0",
        "source_mode": "pinned_remote",
        "uv_lock_sha256": "b" * 64,
        "worktree_clean": True,
    }


class _FakeSystemd:
    def __init__(self) -> None:
        self.enabled: set[str] = set()
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command: tuple[str, ...], _input: bytes | None = None) -> tuple[int, bytes]:
        self.calls.append(command)
        handlers = {
            "disable": self._disable,
            "enable": self._enable,
            "is-active": self._is_active,
            "is-enabled": self._is_enabled,
        }
        return handlers.get(command[2], self._ok)(command)

    def _is_enabled(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        if command[3] in self.enabled:
            return 0, b"enabled\n"
        return 1, b"disabled\n"

    def _is_active(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        if command[3] in self.enabled:
            return 0, b"active\n"
        return 3, b"inactive\n"

    def _enable(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        self.enabled.update(command[4:])
        return 0, b""

    def _disable(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        self.enabled.difference_update(command[4:])
        return 0, b""

    def _ok(self, _command: tuple[str, ...]) -> tuple[int, bytes]:
        return 0, b""


class _FakeLaunchd:
    def __init__(self) -> None:
        self.active: set[str] = set()

    def __call__(self, command: tuple[str, ...], _input: bytes | None = None) -> tuple[int, bytes]:
        handlers = {
            "bootstrap": self._bootstrap,
            "bootout": self._bootout,
            "print": self._print,
        }
        return handlers[command[1]](command)

    def _bootstrap(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        self.active.add(Path(command[3]).stem)
        return 0, b""

    def _bootout(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        self.active.discard(command[2].rsplit("/", 1)[-1])
        return 0, b""

    def _print(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        if command[2].rsplit("/", 1)[-1] in self.active:
            return 0, b"active\n"
        return 113, b"not found\n"
