"""A failed `launchctl print` is not an absent job (audit 2026-09-26, regress 11).

docs/research/2026-09-26-launchd-absent-only-when-launchd-says-so.md
"""
from __future__ import annotations

import install_control


class _Launchctl:
    def __init__(self, print_code: int) -> None:
        self.print_code = print_code
        self.calls: list[str] = []

    def __call__(self, command: tuple[str, ...], _input: bytes | None = None) -> tuple[int, bytes]:
        self.calls.append(command[1])
        if command[1] == "print":
            return self.print_code, b""
        return 0, b""


def test_a_print_that_failed_otherwise_still_gets_its_bootout() -> None:
    runner = _Launchctl(print_code=5)

    install_control._bootout_if_loaded(runner, "launchctl", "gui/501", "llm-wiki-nightly")

    assert runner.calls == ["print", "bootout"]


def test_only_service_not_found_skips_the_bootout() -> None:
    runner = _Launchctl(print_code=install_control.LAUNCHD_SERVICE_NOT_FOUND)

    install_control._bootout_if_loaded(runner, "launchctl", "gui/501", "llm-wiki-nightly")

    assert runner.calls == ["print"]
