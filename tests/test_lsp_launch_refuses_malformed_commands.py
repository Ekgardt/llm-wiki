from __future__ import annotations

import math
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from llm_wiki import lsp_process_launch
from llm_wiki.lsp_process_limits import MAX_GENERATION_LAUNCH_ARGUMENTS
from llm_wiki.lsp_process_state import GenerationLaunch

RESOLVED_INTERPRETER = str(Path(sys.executable).resolve())


def _executable_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def interpreter_descriptor() -> Iterator[int]:
    descriptor = os.open(RESOLVED_INTERPRETER, os.O_RDONLY)
    yield descriptor
    os.close(descriptor)


def test_a_launch_with_more_arguments_than_its_bound_is_refused(tmp_path: Path) -> None:
    too_many = (RESOLVED_INTERPRETER,) + ("-c",) * MAX_GENERATION_LAUNCH_ARGUMENTS
    launch = GenerationLaunch(too_many)

    with pytest.raises(ValueError, match="argument bound"):
        lsp_process_launch.generation_launch((RESOLVED_INTERPRETER,), launch, cwd=tmp_path)


def test_a_launch_argument_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    launch = GenerationLaunch((RESOLVED_INTERPRETER, chr(0xD800)))

    with pytest.raises(ValueError, match="valid UTF-8") as raised:
        lsp_process_launch.generation_launch((RESOLVED_INTERPRETER,), launch, cwd=tmp_path)

    assert isinstance(raised.value.__cause__, UnicodeEncodeError)


def test_a_launch_command_given_as_a_list_is_refused(tmp_path: Path) -> None:
    launch = GenerationLaunch([RESOLVED_INTERPRETER])  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="command must be a tuple"):
        lsp_process_launch.generation_launch((RESOLVED_INTERPRETER,), launch, cwd=tmp_path)


def test_launch_descriptors_given_as_a_list_are_refused(tmp_path: Path) -> None:
    launch = GenerationLaunch((RESOLVED_INTERPRETER,), [3])  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="pass_fds must be a tuple"):
        lsp_process_launch.generation_launch((RESOLVED_INTERPRETER,), launch, cwd=tmp_path)


def test_launch_descriptors_are_refused_where_they_cannot_be_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "nt")

    with pytest.raises(ValueError, match="only on POSIX"):
        lsp_process_launch._launch_pass_fds(GenerationLaunch((), (3,)))


def test_an_empty_launch_cannot_stand_in_for_the_configured_program() -> None:
    with pytest.raises(ValueError, match="cannot replace the configured executable"):
        lsp_process_launch.require_same_program((), ("server",), GenerationLaunch(()))


def test_a_command_with_a_non_string_argument_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="arguments must be strings"):
        lsp_process_launch.validated_command([RESOLVED_INTERPRETER, 3], tmp_path)  # type: ignore[list-item]


def test_an_inherited_descriptor_path_is_kept_as_the_program(
    tmp_path: Path, interpreter_descriptor: int
) -> None:
    descriptor_path = f"/dev/fd/{interpreter_descriptor}"

    command = lsp_process_launch.validated_command([descriptor_path, "--version"], tmp_path)

    assert command == [descriptor_path, "--version"]


def test_a_relative_program_with_a_directory_resolves_against_the_working_directory(
    tmp_path: Path,
) -> None:
    program = _executable_file(tmp_path / "bin" / "server")

    command = lsp_process_launch.validated_command(["bin/server", "--stdio"], tmp_path)

    assert command == [str(program.resolve()), "--stdio"]


def test_a_bare_program_name_is_found_on_the_inherited_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = _executable_file(tmp_path / "tools" / "named-server")
    monkeypatch.setenv("PATH", str(program.parent))

    command = lsp_process_launch.validated_command(["named-server"], tmp_path)

    assert command == [str(program.resolve())]


def test_a_bare_program_name_missing_from_the_path_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(FileNotFoundError, match="absent-server"):
        lsp_process_launch.validated_command(["absent-server"], tmp_path)


def test_a_directory_is_not_a_program(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        lsp_process_launch.validated_command([str(tmp_path)], tmp_path)


@pytest.mark.parametrize("script", ["server.bat", "server.CMD"])
def test_a_windows_shell_script_is_not_a_program(
    script: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "name", "nt")

    with pytest.raises(ValueError, match="shell scripts"):
        lsp_process_launch._check_executable_kind(Path(script), script)


@pytest.mark.parametrize("seconds", [True, "5", None])
def test_a_bootstrap_timeout_that_is_not_seconds_is_refused(seconds: object) -> None:
    with pytest.raises(TypeError, match="must be seconds"):
        lsp_process_launch.validated_bootstrap_timeout(seconds)  # type: ignore[arg-type]


@pytest.mark.parametrize("seconds", [0, -1.5, math.inf, math.nan])
def test_a_bootstrap_timeout_that_is_not_positive_and_finite_is_refused(seconds: float) -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        lsp_process_launch.validated_bootstrap_timeout(seconds)


def test_a_positive_integer_bootstrap_timeout_becomes_float_seconds() -> None:
    assert lsp_process_launch.validated_bootstrap_timeout(3) == 3.0
