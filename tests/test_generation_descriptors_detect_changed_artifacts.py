from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from llm_wiki import generation_descriptors


class _AlreadyAbsolute:
    def __init__(self, text: str) -> None:
        self.text = text

    def absolute(self) -> str:
        return self.text


@pytest.mark.parametrize(
    ("absolute", "expected"),
    [
        ("\\\\?\\C:\\vault\\g1", "\\\\?\\C:\\vault\\g1"),
        ("\\\\server\\share\\g1", "\\\\?\\UNC\\server\\share\\g1"),
        ("/vault/g1", "\\\\?\\/vault/g1"),
    ],
)
def test_a_windows_long_path_names_each_root_form_once(absolute: str, expected: str) -> None:
    windows_path = cast(Path, _AlreadyAbsolute(absolute))
    assert generation_descriptors.windows_long_path(windows_path) == expected


@pytest.mark.parametrize(
    ("attributes", "kind"),
    [(0x00000400 | 0x00000010, "link"), (0x00000010, "directory"), (0x00000020, "file")],
)
def test_a_windows_entry_is_a_link_before_it_is_a_directory(attributes: int, kind: str) -> None:
    assert generation_descriptors._windows_entry_kind(attributes) == kind


def test_a_windows_handle_identity_is_unavailable_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "posix")
    with pytest.raises(OSError, match="Windows handle identity is unavailable"):
        generation_descriptors._windows_handle_file_identity(7)


def test_a_platform_without_stable_identity_cannot_describe_a_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"artifact")
    descriptor = os.open(target, os.O_RDONLY)
    monkeypatch.setattr(os, "name", "unknown-platform")
    try:
        with pytest.raises(OSError, match="stable file identity is unavailable"):
            generation_descriptors.stable_descriptor_state(descriptor)
    finally:
        os.close(descriptor)


def test_closing_an_already_closed_descriptor_after_a_failure_is_quiet(tmp_path: Path) -> None:
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"artifact")
    descriptor = os.open(target, os.O_RDONLY)
    os.close(descriptor)
    generation_descriptors._closed_on_failure(descriptor)


class _WindowsOpenRecorder:
    def __init__(self, failure: str | None) -> None:
        self.failure = failure
        self.closed_handles: list[int] = []
        self.closed_descriptors: list[int] = []

    def open_osfhandle(self, _handle: int, _flags: int) -> int:
        if self.failure == "before-transfer":
            raise OSError("conversion failed")
        return 41

    def set_inheritable(self, _descriptor: int, _inheritable: bool) -> None:
        if self.failure == "after-transfer":
            raise OSError("inheritability failed")


def _install_windows_open(monkeypatch: pytest.MonkeyPatch, recorder: _WindowsOpenRecorder) -> None:
    fake_msvcrt = SimpleNamespace(open_osfhandle=recorder.open_osfhandle)
    monkeypatch.setattr(generation_descriptors, "msvcrt", fake_msvcrt, raising=False)
    monkeypatch.setattr(generation_descriptors, "windows_read_handle", lambda _path: 123)
    monkeypatch.setattr(
        generation_descriptors, "close_handle", recorder.closed_handles.append, raising=False
    )
    monkeypatch.setattr(os, "set_inheritable", recorder.set_inheritable)
    monkeypatch.setattr(os, "close", recorder.closed_descriptors.append)


@pytest.mark.parametrize(
    ("failure", "closed_handles", "closed_descriptors"),
    [("before-transfer", [123], []), ("after-transfer", [], [41])],
)
def test_a_failed_windows_open_closes_exactly_the_owner_it_holds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    closed_handles: list[int],
    closed_descriptors: list[int],
) -> None:
    recorder = _WindowsOpenRecorder(failure)
    _install_windows_open(monkeypatch, recorder)
    with pytest.raises(OSError, match="failed"):
        generation_descriptors.windows_read_descriptor(tmp_path / "artifact.bin")
    monkeypatch.undo()
    assert recorder.closed_handles == closed_handles
    assert recorder.closed_descriptors == closed_descriptors


def test_a_successful_windows_open_keeps_its_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _WindowsOpenRecorder(None)
    _install_windows_open(monkeypatch, recorder)
    descriptor = generation_descriptors.windows_read_descriptor(tmp_path / "artifact.bin")
    monkeypatch.undo()
    assert descriptor == 41
    assert recorder.closed_handles == []
    assert recorder.closed_descriptors == []


def _artifact(tmp_path: Path, payload: bytes) -> Path:
    state_root = tmp_path / "state"
    state_root.mkdir()
    artifact = state_root / "artifact.bin"
    artifact.write_bytes(payload)
    return artifact


def test_an_artifact_larger_than_declared_is_refused(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, b"twelve bytes")
    descriptor = os.open(artifact, os.O_RDONLY)
    try:
        with pytest.raises(ValueError, match="exceeds its declared size"):
            generation_descriptors.hash_descriptor(descriptor, 4)
    finally:
        os.close(descriptor)


def test_an_artifact_opened_as_another_file_is_refused(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, b"artifact")
    other = tmp_path / "other.bin"
    other.write_bytes(b"other")
    descriptor = os.open(artifact, os.O_RDONLY)
    try:
        with pytest.raises(PermissionError, match="identity changed while opening"):
            generation_descriptors._hashed_descriptor(descriptor, other.stat(), 1024)
    finally:
        os.close(descriptor)


def _appending_to(artifact: Path) -> Callable[[], bool]:
    calls: list[bool] = []

    def cancelled() -> bool:
        calls.append(True)
        if len(calls) == 2:
            with artifact.open("ab") as output:
                output.write(b" grown")
        return False

    return cancelled


def test_an_artifact_written_while_it_is_hashed_is_refused(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, b"artifact")
    with pytest.raises(PermissionError, match="artifact changed while hashing"):
        generation_descriptors.hash_artifact(
            artifact, artifact.parent, 1024, cancelled=_appending_to(artifact)
        )


def test_an_unchanged_artifact_hashes_to_its_size_and_digest(tmp_path: Path) -> None:
    import hashlib

    artifact = _artifact(tmp_path, b"artifact")
    total, digest = generation_descriptors.hash_artifact(artifact, artifact.parent, 1024)
    assert (total, digest) == (8, hashlib.sha256(b"artifact").hexdigest())
