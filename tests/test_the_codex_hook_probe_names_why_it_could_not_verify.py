from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from pathlib import Path

import integration_hook_config
import lsp_process_tree
import pytest
from llm_wiki import doctor_integrations as integrations
from llm_wiki import doctor_schedule

from tests.slow_machine import SHORT_TIMEOUT

_Hook = dict[str, object]


class _Process:
    def __init__(self, stdin: io.BytesIO | None, stdout: object) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = io.BytesIO(b"")
        self.returncode = 0
        self.killed = 0
        self.wait_error: BaseException | None = None

    def kill(self) -> None:
        self.killed += 1

    def wait(self, timeout: float) -> int:
        if self.wait_error is not None:
            raise self.wait_error
        return self.returncode


class _Tree:
    def __init__(self, process: _Process, failure: BaseException | None = None) -> None:
        self.process = process
        self.failure = failure
        self.terminated = 0
        self.closed = 0

    def terminate(self, deadline: float) -> None:
        self.terminated += 1
        if self.failure is not None:
            raise self.failure

    def close(self) -> None:
        self.closed += 1
        if self.failure is not None:
            raise self.failure


class _FailingStream:
    def read1(self, _size: int) -> bytes:
        raise OSError("pipe broken")


def _tree(failure: BaseException | None = None) -> _Tree:
    return _Tree(_Process(io.BytesIO(), io.BytesIO(b"")), failure)


def test_a_tree_that_refuses_to_terminate_does_not_escape_the_probe() -> None:
    tree = _tree(OSError("gone"))
    integrations._kill_and_reap(tree)
    assert tree.terminated == 1


def test_a_stream_that_breaks_while_read_marks_the_probe_overflowed() -> None:
    process = _Process(io.BytesIO(), _FailingStream())
    streams = integrations._start_codex_readers(process)
    for reader in streams.readers:
        reader.join()
    assert streams.overflow.is_set()
    assert process.killed == 1


def test_a_peer_that_does_not_exit_in_time_is_killed() -> None:
    tree = _tree()
    tree.process.wait_error = subprocess.TimeoutExpired("codex", 0)
    assert integrations._finish_codex_process(tree, time.monotonic()) is False
    assert tree.terminated == 1


def test_readers_still_running_at_the_deadline_leave_the_probe_incomplete() -> None:
    release = threading.Event()
    reader = threading.Thread(target=release.wait, daemon=True)
    reader.start()
    streams = integrations._CodexProbeStreams(
        [reader], {"stdout": bytearray(), "stderr": bytearray()}, threading.Event()
    )
    tree = _tree()
    outcome = integrations._drained_codex_output(tree, streams, time.monotonic() - 1.0)
    release.set()
    reader.join()
    assert outcome is doctor_schedule.PROBE_INCOMPLETE
    assert tree.terminated == 1


def test_a_peer_that_cannot_be_spawned_leaves_the_probe_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def refuse(*_args: object, **_options: object) -> object:
        raise OSError("no codex")

    monkeypatch.setattr(lsp_process_tree.ProcessTree, "spawn_with_deadline", refuse)
    outcome = integrations.run_codex_probe(
        ["codex"], tmp_path, tmp_path, time.monotonic() + SHORT_TIMEOUT
    )
    assert outcome is doctor_schedule.PROBE_INCOMPLETE


def _spawning(monkeypatch: pytest.MonkeyPatch, tree: _Tree) -> None:
    def spawn(*_args: object, **_options: object) -> _Tree:
        return tree

    monkeypatch.setattr(lsp_process_tree.ProcessTree, "spawn_with_deadline", spawn)


def test_a_probe_that_fails_kills_the_tree_and_survives_a_failed_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = _tree(OSError("gone"))
    _spawning(monkeypatch, tree)

    def fail(*_args: object) -> object:
        raise ValueError("garbled")

    monkeypatch.setattr(integrations, "_probed_codex_tree", fail)
    outcome = integrations.run_codex_probe(
        ["codex"], tmp_path, tmp_path, time.monotonic() + SHORT_TIMEOUT
    )
    assert outcome is doctor_schedule.PROBE_INCOMPLETE
    assert (tree.terminated, tree.closed) == (1, 1)


def test_a_peer_without_its_pipes_is_killed_before_it_is_asked() -> None:
    tree = _Tree(_Process(None, io.BytesIO(b"")))
    outcome = integrations._probed_codex_tree(tree, b"payload\n", time.monotonic() + SHORT_TIMEOUT)
    assert outcome is doctor_schedule.PROBE_INCOMPLETE
    assert tree.terminated == 1


@pytest.mark.parametrize(
    "raw",
    [b'{"id":2,"result":[]}\n', b"\xff", b"{not json\n", b'{"id":1,"result":{}}\n'],
    ids=["result-not-an-object", "undecodable", "not-json", "no-hooks-answer"],
)
def test_an_answer_without_a_hook_list_reads_as_nothing(raw: bytes) -> None:
    assert integrations.codex_hooks_result(raw) is None


def _template(root: Path, value: object) -> Path:
    path = root / "integrations" / "codex" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def _stop_template(handler: object) -> dict[str, object]:
    return {"hooks": {"Stop": [{"hooks": [handler]}]}}


@pytest.mark.parametrize(
    "value",
    [
        ["hooks"],
        {"hooks": []},
        {"hooks": {"Stop": {}}},
        _stop_template("not an object"),
        _stop_template({"type": "command"}),
    ],
    ids=["not-an-object", "hooks-not-a-table", "groups-not-a-list", "handler-text", "no-command"],
)
def test_a_template_the_installer_did_not_write_is_refused(tmp_path: Path, value: object) -> None:
    with pytest.raises(ValueError, match="invalid Codex hook template"):
        integrations.expected_codex_runtime_hooks(_template(tmp_path, value))


def test_a_rendered_template_with_a_non_text_event_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def rendered(*_args: object) -> dict[str, object]:
        return {"hooks": {1: []}}

    monkeypatch.setattr(integration_hook_config, "codex_rendered_template", rendered)
    path = _template(tmp_path, _stop_template({"command": "cmd", "commandWindows": "cmd"}))
    with pytest.raises(ValueError, match="invalid Codex hook template"):
        integrations.expected_codex_runtime_hooks(path, {"provider": "fake"})


@pytest.mark.parametrize(
    "response",
    [{"data": {}}, {"data": []}, {"data": [1]}],
    ids=["data-not-a-list", "no-entry", "entry-not-an-object"],
)
def test_a_probe_answer_without_exactly_one_entry_is_invalid(response: object) -> None:
    assert integrations._codex_probe_entry(response) == (None, "runtime_hooks_invalid")


def test_a_probe_entry_for_another_directory_is_named(tmp_path: Path) -> None:
    entry = {"cwd": str(tmp_path / "elsewhere"), "hooks": []}
    verdict = integrations._codex_entry_hooks_verdict(entry, tmp_path)
    assert verdict == (False, "runtime_hooks_wrong_cwd")


@pytest.mark.parametrize(
    "entry",
    [{"cwd": 5, "hooks": []}, {"hooks": "hooks"}],
    ids=["cwd-not-a-path", "hooks-not-a-list"],
)
def test_a_probe_entry_of_the_wrong_shape_is_invalid(
    tmp_path: Path, entry: dict[str, object]
) -> None:
    located = {"cwd": str(tmp_path), **entry}
    verdict = integrations._codex_entry_hooks_verdict(located, tmp_path)
    assert verdict == (False, "runtime_hooks_invalid")


def test_hooks_without_their_template_are_refused(tmp_path: Path) -> None:
    verdict = integrations.codex_hooks_verdict(tmp_path, [])
    assert verdict == (False, "runtime_hooks_template_invalid")


def test_a_different_number_of_our_hooks_than_the_template_is_a_mismatch(tmp_path: Path) -> None:
    _template(tmp_path, _stop_template({"command": "cmd", "commandWindows": "cmd"}))
    assert integrations.codex_hooks_verdict(tmp_path, []) == (False, "runtime_hooks_mismatch")


def test_a_hook_of_unknown_trust_is_named_as_such(tmp_path: Path) -> None:
    wanted: _Hook = {"eventName": "Stop", "matcher": None, "command": "cmd"}
    hook: _Hook = {**wanted, "enabled": True, "trustStatus": "pending"}
    problem = integrations.codex_hook_problem(wanted, [hook], tmp_path)
    assert problem == "runtime_hooks_trust_unknown"


def test_a_host_without_its_configuration_is_refused() -> None:
    with pytest.raises(ValueError, match="missing integration host configuration"):
        integrations._required_host_config(None)
