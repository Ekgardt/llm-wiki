from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import installer_config
import pytest
from install_state import MANIFEST_SCHEMA, MAX_RECORD_BYTES, InstallControlError, ManagedResource
from llm_wiki import install_records
from llm_wiki.formats.canonical import canonical_json_bytes

from tests.slow_machine import SHORT_TIMEOUT


def _resource(kind: str, resource_id: str = "thing") -> ManagedResource:
    return ManagedResource(
        resource_id=resource_id,
        kind=kind,
        locator="somewhere",
        desired=b"value",
        read_owned=lambda: None,
        write_owned=lambda _value: None,
        recognizes=lambda _value: True,
    )


def _runner_answering(exit_code: int, output: bytes) -> install_records.CommandRunner:
    def run(_command: tuple[str, ...], _input: bytes | None) -> tuple[int, bytes]:
        return exit_code, output

    return run


@pytest.fixture
def held_lock(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "install.lock"
    with install_records.install_lock(path, SHORT_TIMEOUT):
        yield path


def test_a_record_larger_than_the_bound_is_never_written(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    with pytest.raises(InstallControlError, match="install_record_too_large"):
        install_records.write_record(path, {"filler": "x" * (MAX_RECORD_BYTES + 1)})
    assert not path.exists()


def test_a_record_written_within_the_bound_reads_back_canonical(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    install_records.write_record(path, {"b": 1, "a": "two"})
    assert path.read_bytes() == canonical_json_bytes({"a": "two", "b": 1})


def test_a_canonical_record_that_breaks_its_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_bytes(canonical_json_bytes({"schema": "install-manifest/v1"}))
    with pytest.raises(InstallControlError, match="install_state_schema_invalid"):
        install_records.read_record(path, MANIFEST_SCHEMA)


def test_an_absent_optional_record_reads_as_none(tmp_path: Path) -> None:
    assert install_records.optional_record(tmp_path / "missing.json", MANIFEST_SCHEMA) is None


def test_a_lock_held_by_another_descriptor_times_out(held_lock: Path) -> None:
    with (
        pytest.raises(InstallControlError, match="install_lock_timeout"),
        install_records.install_lock(held_lock, 0.1),
    ):
        pytest.fail("the lock was granted while another descriptor held it")


def test_a_free_lock_is_taken_and_seeded(tmp_path: Path) -> None:
    path = tmp_path / "install.lock"
    with install_records.install_lock(path, SHORT_TIMEOUT):
        seeded = path.read_bytes()
    assert seeded == b"0"


@pytest.mark.skipif(os.name != "posix", reason="Windows has no owner-only file mode bits")
def test_a_free_lock_is_kept_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "install.lock"
    path.write_bytes(b"0")
    path.chmod(0o644)
    with install_records.install_lock(path, SHORT_TIMEOUT):
        pass
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_command_given_input_answers_with_its_own_exit_code() -> None:
    command = (sys.executable, "-c", "import sys; sys.exit(len(sys.stdin.buffer.read()))")
    assert install_records.default_command_runner(command, b"abc") == (3, b"")


def test_a_command_that_cannot_start_answers_as_timed_out(tmp_path: Path) -> None:
    command = (str(tmp_path / "no-such-program"),)
    assert install_records.default_command_runner(command, b"input") == (124, b"")


def test_a_command_given_input_that_times_out_answers_124(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def time_out(command: Sequence[str], **_options: object) -> None:
        raise subprocess.TimeoutExpired(command, 30.0)

    monkeypatch.setattr(subprocess, "run", time_out)
    assert install_records.default_command_runner(("anything",), b"input") == (124, b"")


def test_a_command_without_input_returns_what_it_printed() -> None:
    command = (sys.executable, "-c", "print('reported')")
    exit_code, output = install_records.default_command_runner(command)
    assert (exit_code, output.strip()) == (0, b"reported")


def test_a_command_without_input_that_is_not_proven_finished_answers_124(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Mapping[str, object]] = []

    def unproven(command: Sequence[str], **options: object) -> None:
        seen.append({"command": tuple(command), **options})

    monkeypatch.setattr(installer_config, "bounded_process_output", unproven)
    assert install_records.default_command_runner(("probe", "now")) == (124, b"")
    assert seen[0]["command"] == ("probe", "now")
    assert seen[0]["max_bytes"] == MAX_RECORD_BYTES
    assert seen[0]["timeout_seconds"] == 30.0


def test_a_teardown_step_with_oversized_output_is_refused() -> None:
    runner = _runner_answering(1, b"x" * (MAX_RECORD_BYTES + 1))
    with pytest.raises(InstallControlError, match="install_scheduler_output_oversized"):
        install_records.try_command(runner, ("teardown",))


def test_a_teardown_step_that_fails_quietly_settles_nothing() -> None:
    install_records.try_command(_runner_answering(5, b"not registered"), ("teardown",))


def test_a_required_command_that_fails_is_refused() -> None:
    with pytest.raises(InstallControlError, match="install_scheduler_command_failed"):
        install_records.require_command(_runner_answering(1, b""), ("reload",))


def test_a_required_command_with_oversized_output_is_refused() -> None:
    runner = _runner_answering(0, b"x" * (MAX_RECORD_BYTES + 1))
    with pytest.raises(InstallControlError, match="install_scheduler_output_oversized"):
        install_records.require_command(runner, ("reload",))


def test_a_required_command_returns_its_output() -> None:
    runner = _runner_answering(0, b"fine")
    assert install_records.require_command(runner, ("reload",)) == b"fine"


@pytest.mark.parametrize("kind", ["windows_task_scheduler", "cron_scheduler"])
def test_a_changed_unreadable_scheduler_asks_for_a_hand_repair(kind: str) -> None:
    step = install_records.drift_next_step(_resource(kind))
    assert step.startswith("restore it or remove it by hand")
    assert "install_control.py uninstall" in step


def test_a_changed_adoptable_resource_names_the_adopt_flag() -> None:
    step = install_records.drift_next_step(_resource("profile_fragment", "unix-profile"))
    assert "--adopt unix-profile" in step


def test_a_drift_error_keeps_its_bare_code_and_explains_the_way_on() -> None:
    error = install_records.ResourceDriftError("install_drift", _resource("cron_scheduler"))
    assert install_records.error_code(error) == "install_drift"
    assert "thing (somewhere) was changed outside the installer" in str(error)
