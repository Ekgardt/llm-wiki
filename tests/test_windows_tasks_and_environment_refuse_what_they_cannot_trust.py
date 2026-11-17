from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest
from install_state import (
    MAX_PREIMAGE_BYTES,
    MAX_RECORD_BYTES,
    InstallControlError,
    ManagedResource,
    resource_snapshot,
)
from llm_wiki import install_windows
from llm_wiki.formats.canonical import canonical_json_bytes

_Answer = tuple[int, bytes]


class _Roots:
    def __init__(self, base: Path) -> None:
        self.root = base / "vault"
        self.state_root = base / "state"
        self.uv_path = base / "bin" / "uv"


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Roots:
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
    return _Roots(tmp_path)


def _spec(roots: _Roots) -> dict[str, object]:
    rendered = install_windows.render_windows_task_spec(roots.root, roots.state_root, roots.uv_path)
    decoded: dict[str, object] = json.loads(rendered)
    return decoded


def _decode(roots: _Roots, value: dict[str, object]) -> dict[str, object]:
    return install_windows.decode_windows_task_spec(
        canonical_json_bytes(value), roots.root, roots.state_root
    )


def _task_resource(roots: _Roots, answers: list[_Answer]) -> ManagedResource:
    def runner(_command: tuple[str, ...], _input: bytes | None) -> _Answer:
        return answers.pop(0)

    return install_windows.windows_task_scheduler_resource(
        root=roots.root,
        state_root=roots.state_root,
        uv_path=roots.uv_path,
        script_path=roots.root / "scripts" / "install-scheduled-tasks.ps1",
        powershell="pwsh",
        runner=runner,
    )


def test_the_user_environment_is_unavailable_off_windows(
    roots: _Roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_windows.sys, "platform", "linux")
    resource = install_windows.windows_environment_resources(roots.root, roots.state_root)[0]
    with pytest.raises(InstallControlError, match="install_windows_environment_unavailable"):
        resource.read_owned()
    with pytest.raises(InstallControlError, match="install_windows_environment_unavailable"):
        resource.write_owned(None)


def test_an_injected_environment_reads_and_writes_text(roots: _Roots) -> None:
    store: dict[str, str | None] = {}
    resource = install_windows.windows_environment_resources(
        roots.root, roots.state_root, read_value=store.get, write_value=store.__setitem__
    )[0]
    assert resource.read_owned() is None
    resource.write_owned(resource.desired)
    assert store == {"LLM_WIKI_ROOT": str(roots.root.resolve())}
    assert resource.read_owned() == resource.desired
    resource.write_owned(None)
    assert store == {"LLM_WIKI_ROOT": None}


def _registry(deleted: list[tuple[object, str]], missing: bool) -> ModuleType:
    def delete_value(key: object, name: str) -> None:
        deleted.append((key, name))
        if missing:
            raise FileNotFoundError(name)

    registry = ModuleType("winreg")
    vars(registry)["DeleteValue"] = delete_value
    return registry


@pytest.mark.parametrize("missing", [False, True])
def test_deleting_a_user_variable_tolerates_one_already_gone(missing: bool) -> None:
    deleted: list[tuple[object, str]] = []
    install_windows._windows_delete_user_env(_registry(deleted, missing), "key", "LLM_WIKI_ROOT")
    assert deleted == [("key", "LLM_WIKI_ROOT")]


def test_the_rendered_spec_decodes_to_itself(roots: _Roots) -> None:
    assert _decode(roots, _spec(roots)) == _spec(roots)


def test_a_spec_larger_than_a_preimage_is_refused(roots: _Roots) -> None:
    with pytest.raises(InstallControlError, match="install_windows_task_spec_invalid"):
        install_windows.decode_windows_task_spec(
            b" " * (MAX_PREIMAGE_BYTES + 1), roots.root, roots.state_root
        )


def _with(roots: _Roots, key: str, value: object) -> dict[str, object]:
    return {**_spec(roots), key: value}


def _with_task(roots: _Roots, task: object) -> dict[str, object]:
    tasks = _spec(roots)["tasks"]
    assert isinstance(tasks, list)
    return _with(roots, "tasks", [task, tasks[1]])


def _first_task_with(roots: _Roots, key: str, value: object) -> dict[str, object]:
    tasks = _spec(roots)["tasks"]
    assert isinstance(tasks, list)
    return _with_task(roots, {**tasks[0], key: value})


@pytest.mark.parametrize(
    "case",
    [
        "tasks-not-a-list",
        "one-task",
        "task-not-an-object",
        "zero-hour-limit",
        "renamed-task",
        "unknown-version",
        "extra-key",
        "relative-root",
        "other-root",
        "relative-uv",
    ],
)
def test_a_spec_the_installer_did_not_render_is_refused(roots: _Roots, case: str) -> None:
    cases: dict[str, dict[str, object]] = {
        "tasks-not-a-list": _with(roots, "tasks", {}),
        "one-task": _with(roots, "tasks", []),
        "task-not-an-object": _with_task(roots, "nightly"),
        "zero-hour-limit": _first_task_with(roots, "limit_hours", 0),
        "renamed-task": _first_task_with(roots, "name", "Other"),
        "unknown-version": _with(roots, "spec", 3),
        "extra-key": _with(roots, "extra", True),
        "relative-root": _with(roots, "root", "vault"),
        "other-root": _with(roots, "root", str(roots.state_root.resolve())),
        "relative-uv": _with(roots, "uv_path", "uv"),
    }
    with pytest.raises(InstallControlError, match="install_windows_task_spec_invalid"):
        _decode(roots, cases[case])


def _state(value: object) -> bytes:
    return json.dumps(value).encode()


def test_installed_tasks_read_back_as_the_rendered_spec(roots: _Roots) -> None:
    resource = _task_resource(roots, [(0, _state({"state": "equivalent"}))])
    assert resource.read_owned() == resource.desired


def test_absent_tasks_read_back_as_nothing(roots: _Roots) -> None:
    resource = _task_resource(roots, [(0, _state({"state": "absent"}))])
    assert resource.read_owned() is None


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        ((1, b""), "install_windows_task_state_failed"),
        ((0, b"x" * (MAX_RECORD_BYTES + 1)), "install_scheduler_output_oversized"),
        ((0, b"{not json"), "install_windows_task_state_invalid"),
        ((0, b"\xff"), "install_windows_task_state_invalid"),
        ((0, _state(["equivalent"])), "install_windows_task_state_invalid"),
        ((0, _state({"state": "running"})), "install_windows_task_state_invalid"),
    ],
    ids=["exit-code", "oversized", "bad-json", "undecodable", "not-an-object", "unknown-state"],
)
def test_a_task_state_that_cannot_be_trusted_is_refused(
    roots: _Roots, answer: _Answer, code: str
) -> None:
    resource = _task_resource(roots, [answer])
    with pytest.raises(InstallControlError, match=code):
        resource.read_owned()


def _recover(resource: ManagedResource, snapshot: dict[str, object]) -> tuple[bytes, bytes]:
    recover = resource.recover_legacy_projection
    assert recover is not None
    return recover(snapshot)


def _persist(roots: _Roots, value: bytes) -> None:
    path = roots.state_root / "run" / "install" / "scheduler" / "windows" / "tasks.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(value)


def test_a_legacy_spec_that_was_never_persisted_cannot_be_recovered(roots: _Roots) -> None:
    resource = _task_resource(roots, [])
    with pytest.raises(InstallControlError, match="install_legacy_definition_missing"):
        _recover(resource, resource_snapshot(resource.desired))


def test_a_persisted_spec_that_differs_from_the_record_is_refused(roots: _Roots) -> None:
    resource = _task_resource(roots, [])
    _persist(roots, resource.desired)
    with pytest.raises(InstallControlError, match="install_legacy_definition_mismatch"):
        _recover(resource, resource_snapshot(b"another spec"))


def test_a_persisted_spec_matching_the_record_is_recovered(roots: _Roots) -> None:
    resource = _task_resource(roots, [])
    _persist(roots, resource.desired)
    assert _recover(resource, resource_snapshot(resource.desired)) == (
        resource.desired,
        resource.desired,
    )
