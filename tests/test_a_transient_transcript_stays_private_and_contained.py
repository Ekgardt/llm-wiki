from __future__ import annotations

import os
import secrets
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from event_envelope import EventEnvelope, build_event_envelope
from llm_wiki import integration_transient_files as transient
from llm_wiki.integration_settings import TRANSIENT_CREATE_ATTEMPTS

IcaclsCall = list[str]

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX file modes, directory descriptors and renames of an open file",
)


@pytest.fixture
def envelope() -> EventEnvelope:
    return build_event_envelope(
        event_type="session_start", payload={"reason": None}, agent="claude", session="s1"
    )


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(root))
    return root.resolve()


def _transient_dir(state_root: Path) -> Path:
    return state_root / "cache" / "transient-transcripts"


def _private_parent(state_root: Path, mode: int) -> Path:
    parent = _transient_dir(state_root)
    parent.mkdir(parents=True, mode=0o700)
    parent.parent.chmod(0o700)
    parent.chmod(mode)
    return parent


def _token_sequence(monkeypatch: pytest.MonkeyPatch, tokens: list[str]) -> None:
    remaining: Iterator[str] = iter(tokens)
    monkeypatch.setattr(secrets, "token_hex", lambda _size: next(remaining))


def _completed(returncode: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(["icacls"], returncode, b"", b"")


def _answer_icacls(
    monkeypatch: pytest.MonkeyPatch, calls: list[IcaclsCall], returncode: int
) -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return _completed(returncode)

    monkeypatch.setattr(subprocess, "run", run)


def _restrict_with(monkeypatch: pytest.MonkeyPatch, on_file: Callable[[Path], None]) -> None:
    def restrict(target: Path) -> None:
        if target.is_file():
            on_file(target)

    monkeypatch.setattr(transient, "restrict_file_permissions", restrict)


def _secured_parent() -> tuple[Path, os.stat_result]:
    _root, parent, info = transient._secure_transient_dir()
    return parent, info


def _write_on_windows_path(envelope: EventEnvelope, state_root: Path, text: str) -> Path:
    parent, info = _secured_parent()
    return transient._write_windows_transient(envelope, text, state_root, parent, info)


def _refuse(_target: Path) -> None:
    raise PermissionError("transient permissions unavailable")


def _replace_with_stranger(target: Path) -> None:
    target.unlink()
    target.write_text("stranger", encoding="utf-8")


@posix_only
def test_a_written_transcript_is_owner_only_and_holds_the_text(
    state_root: Path, envelope: EventEnvelope
) -> None:
    created = transient.write_transient_transcript(envelope, "private words")

    assert created.parent == _transient_dir(state_root)
    assert created.read_text(encoding="utf-8") == "private words"
    assert stat.S_IMODE(created.stat().st_mode) == 0o600
    assert stat.S_IMODE(created.parent.stat().st_mode) == 0o700


@posix_only
def test_a_group_readable_parent_is_tightened_before_writing(
    state_root: Path, envelope: EventEnvelope
) -> None:
    parent = _private_parent(state_root, 0o750)

    created = transient.write_transient_transcript(envelope, "text")

    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert created.read_text(encoding="utf-8") == "text"


@posix_only
def test_a_parent_whose_mode_does_not_hold_is_refused(
    state_root: Path, envelope: EventEnvelope, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _private_parent(state_root, 0o750)
    monkeypatch.setattr(Path, "chmod", lambda _self, _mode: None)

    with pytest.raises(PermissionError, match="not private"):
        transient.write_transient_transcript(envelope, "text")

    assert not list(parent.iterdir())


def test_a_file_where_the_parent_belongs_is_refused(
    state_root: Path, envelope: EventEnvelope
) -> None:
    (state_root / "cache").mkdir(mode=0o700)
    _transient_dir(state_root).write_text("not a directory", encoding="utf-8")

    with pytest.raises(PermissionError, match="not secure"):
        transient.write_transient_transcript(envelope, "text")


def test_a_directory_outside_the_state_root_has_escaped(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(PermissionError, match="escaped state root"):
        transient._validate_transient_containment(tmp_path / "state", outside)


def test_a_directory_replaced_since_it_was_checked_is_refused(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    with pytest.raises(PermissionError, match="identity changed"):
        transient._validate_transient_dir(tmp_path, second, first.lstat())


@posix_only
def test_a_parent_descriptor_that_is_not_a_directory_is_refused(tmp_path: Path) -> None:
    regular = tmp_path / "regular"
    regular.write_text("x", encoding="utf-8")
    descriptor = os.open(regular, os.O_RDONLY)
    try:
        with pytest.raises(PermissionError, match="identity changed"):
            transient._validate_posix_parent_fd(descriptor, regular.lstat())
    finally:
        os.close(descriptor)


@posix_only
def test_a_parent_descriptor_of_another_directory_is_refused(tmp_path: Path) -> None:
    opened = tmp_path / "opened"
    expected = tmp_path / "expected"
    opened.mkdir()
    expected.mkdir()
    descriptor = os.open(opened, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(PermissionError, match="identity changed"):
            transient._validate_posix_parent_fd(descriptor, expected.lstat())
    finally:
        os.close(descriptor)


@posix_only
def test_a_transcript_descriptor_that_is_not_a_regular_file_is_refused(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    info = tmp_path.lstat()
    try:
        with pytest.raises(PermissionError, match="not regular"):
            transient._write_open_posix_transient(
                descriptor, descriptor, "name", "text", tmp_path, tmp_path, info
            )
    finally:
        os.close(descriptor)


@posix_only
def test_a_write_that_makes_no_progress_leaves_no_transcript(
    state_root: Path, envelope: EventEnvelope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "write", lambda _descriptor, _data: 0)

    with pytest.raises(OSError, match="transient write failed"):
        transient.write_transient_transcript(envelope, "text")

    assert not list(_transient_dir(state_root).iterdir())


@posix_only
def test_a_transcript_swapped_during_the_write_is_refused_and_the_stranger_kept(
    state_root: Path, envelope: EventEnvelope, monkeypatch: pytest.MonkeyPatch
) -> None:
    _token_sequence(monkeypatch, ["swap"])
    real_fsync = os.fsync
    name = f"{envelope.event_id}-swap.txt"

    def fsync_and_swap(descriptor: int) -> None:
        real_fsync(descriptor)
        written = _transient_dir(state_root) / name
        written.rename(state_root / "moved.txt")
        written.write_text("stranger", encoding="utf-8")

    monkeypatch.setattr(os, "fsync", fsync_and_swap)

    with pytest.raises(PermissionError, match="file identity changed"):
        transient.write_transient_transcript(envelope, "text")

    assert (_transient_dir(state_root) / name).read_text(encoding="utf-8") == "stranger"


@posix_only
def test_every_name_taken_ends_the_posix_attempts(
    state_root: Path, envelope: EventEnvelope, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _private_parent(state_root, 0o700)
    (parent / f"{envelope.event_id}-taken.txt").write_text("existing", encoding="utf-8")
    _token_sequence(monkeypatch, ["taken"] * TRANSIENT_CREATE_ATTEMPTS)

    with pytest.raises(FileExistsError, match="unique transient transcript"):
        transient.write_transient_transcript(envelope, "text")


@posix_only
def test_an_unlink_of_an_unopened_transcript_does_nothing(tmp_path: Path) -> None:
    kept = tmp_path / "kept.txt"
    kept.write_text("kept", encoding="utf-8")
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        transient._unlink_posix_transient(descriptor, None, None)
        transient._unlink_posix_transient(descriptor, "kept.txt", tmp_path.lstat())
    finally:
        os.close(descriptor)

    assert kept.read_text(encoding="utf-8") == "kept"


def test_a_missing_file_is_not_the_opened_one(tmp_path: Path) -> None:
    assert transient._same_file(tmp_path / "missing", tmp_path.lstat()) is False


@posix_only
def test_a_missing_name_under_a_directory_is_not_the_opened_one(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert transient._same_file_at(descriptor, "missing", tmp_path.lstat()) is False
    finally:
        os.close(descriptor)


@posix_only
def test_posix_permissions_are_owner_read_and_write(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o644)

    transient.restrict_file_permissions(target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_windows_writes_the_text_through_a_restricted_file(
    state_root: Path, envelope: EventEnvelope, monkeypatch: pytest.MonkeyPatch
) -> None:
    restricted: list[Path] = []
    _restrict_with(monkeypatch, restricted.append)

    created = _write_on_windows_path(envelope, state_root, "windows words")

    assert created.read_text(encoding="utf-8") == "windows words"
    assert restricted == [created]


def test_windows_refuses_a_file_it_cannot_restrict_and_removes_it(
    state_root: Path, envelope: EventEnvelope, monkeypatch: pytest.MonkeyPatch
) -> None:
    _restrict_with(monkeypatch, _refuse)

    with pytest.raises(PermissionError, match="permissions unavailable"):
        _write_on_windows_path(envelope, state_root, "text")

    assert not list(_transient_dir(state_root).iterdir())


def test_windows_keeps_a_stranger_that_replaced_the_file(
    state_root: Path, envelope: EventEnvelope, monkeypatch: pytest.MonkeyPatch
) -> None:
    _restrict_with(monkeypatch, _replace_with_stranger)
    _token_sequence(monkeypatch, ["swap"])

    with pytest.raises(PermissionError, match="file identity changed"):
        _write_on_windows_path(envelope, state_root, "text")

    stranger = _transient_dir(state_root) / f"{envelope.event_id}-swap.txt"
    assert stranger.read_text(encoding="utf-8") == "stranger"


def test_windows_refuses_a_file_swapped_during_the_write(
    state_root: Path, envelope: EventEnvelope, monkeypatch: pytest.MonkeyPatch
) -> None:
    _restrict_with(monkeypatch, lambda _target: None)
    _token_sequence(monkeypatch, ["swap"])
    real_fsync = os.fsync

    def fsync_and_swap(descriptor: int) -> None:
        real_fsync(descriptor)
        _replace_with_stranger(_transient_dir(state_root) / f"{envelope.event_id}-swap.txt")

    monkeypatch.setattr(os, "fsync", fsync_and_swap)

    with pytest.raises(PermissionError, match="file identity changed"):
        _write_on_windows_path(envelope, state_root, "text")

    stranger = _transient_dir(state_root) / f"{envelope.event_id}-swap.txt"
    assert stranger.read_text(encoding="utf-8") == "stranger"


def test_a_windows_parent_is_restricted_and_keeps_its_identity(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _private_parent(state_root, 0o700)
    restricted: list[Path] = []
    monkeypatch.setattr(transient, "restrict_file_permissions", restricted.append)

    info = transient._secure_windows_parent(state_root, parent, parent.lstat())

    assert restricted == [parent]
    assert (info.st_dev, info.st_ino) == (parent.lstat().st_dev, parent.lstat().st_ino)


def test_icacls_grants_the_user_read_write_and_delete_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[IcaclsCall] = []
    monkeypatch.setenv("USERNAME", "Test User")
    _answer_icacls(monkeypatch, calls, 0)

    transient._restrict_windows_permissions(tmp_path)

    assert calls == [["icacls", str(tmp_path), "/inheritance:r", "/grant:r", "Test User:(R,W,D)"]]


def test_a_failed_icacls_leaves_the_permissions_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERNAME", "Test User")
    _answer_icacls(monkeypatch, [], 5)

    with pytest.raises(PermissionError, match="permissions unavailable"):
        transient._restrict_windows_permissions(tmp_path)


def test_windows_retries_a_taken_name_then_gives_up(
    state_root: Path, envelope: EventEnvelope, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _private_parent(state_root, 0o700)
    (parent / f"{envelope.event_id}-taken.txt").write_text("existing", encoding="utf-8")
    _token_sequence(monkeypatch, ["taken"] * TRANSIENT_CREATE_ATTEMPTS)

    with pytest.raises(FileExistsError, match="unique transient transcript"):
        transient._open_windows_transient(parent, envelope.event_id)

    assert [path.name for path in parent.iterdir()] == [f"{envelope.event_id}-taken.txt"]


def test_windows_permissions_need_a_user_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("USERNAME", raising=False)

    with pytest.raises(PermissionError, match="permissions unavailable"):
        transient._restrict_windows_permissions(tmp_path)


def test_windows_refuses_an_opened_directory_as_a_transcript(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="not regular"):
        transient._validate_windows_opened(tmp_path, tmp_path.lstat(), tmp_path, tmp_path)


def test_windows_refuses_a_transcript_outside_its_parent(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    created = tmp_path / "created.txt"
    created.write_text("x", encoding="utf-8")

    with pytest.raises(PermissionError, match="containment changed"):
        transient._validate_windows_opened(created, created.lstat(), tmp_path, elsewhere)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\\\\?\\UNC\\server\\share\\file.txt", "\\\\server\\share\\file.txt"),
        ("\\\\?\\C:\\state\\file.txt", "C:\\state\\file.txt"),
        ("C:\\state\\file.txt", "C:\\state\\file.txt"),
    ],
)
def test_a_final_windows_path_loses_its_extended_prefix(raw: str, expected: str) -> None:
    assert str(transient._normalize_windows_path(raw)) == expected


def test_no_final_path_is_read_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")

    assert transient._windows_path_from_fd(0) is None


def test_no_final_path_without_the_windows_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "msvcrt", None)
    monkeypatch.setattr(os, "name", "nt")

    assert transient._windows_path_from_fd(0) is None


def test_no_final_path_when_the_windows_api_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transient, "_raw_windows_path_from_fd", lambda _descriptor: None)
    monkeypatch.setattr(os, "name", "nt")

    assert transient._windows_path_from_fd(0) is None


def test_the_final_windows_path_is_normalized_when_the_api_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "\\\\?\\C:\\state\\file.txt"
    monkeypatch.setattr(transient, "_raw_windows_path_from_fd", lambda _descriptor: raw)
    monkeypatch.setattr(os, "name", "nt")

    assert str(transient._windows_path_from_fd(0)) == "C:\\state\\file.txt"
