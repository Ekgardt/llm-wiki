"""The doctor reads LSP owner, lease and failure records under deadlines, fails closed on anything malformed, and dates crash evidence."""

from __future__ import annotations

import errno
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from llm_wiki import monotonic_deadline

from tests.doctor_case_helpers import (
    _write_lsp_lease,
    give_doctor_a_generous_budget,
)
from tests.doctor_helpers import (
    _lsp_timestamp,
    _write_lsp_failure,
    _write_lsp_owner,
)
from tests.slow_machine import SHORT_TIMEOUT

_budget_that_survives_a_slow_runner = pytest.fixture(autouse=True)(give_doctor_a_generous_budget)


def test_doctor_lsp_explains_missing_identity_without_allowing_cleanup(tmp_path) -> None:
    from llm_wiki import doctor_lsp

    now = datetime.now(UTC)
    owner = _write_lsp_owner(
        tmp_path, owner_nonce="a" * 32, generation_nonce="b" * 32,
        started_at=now - timedelta(days=32), owner_pid=os.getpid(),
    )
    _write_lsp_failure(
        owner, owner_nonce="a" * 32, generation_nonce="b" * 32,
        timestamp=now - timedelta(days=31),
    )
    before = (owner / "owner.json").read_bytes()
    check = doctor_lsp.lsp_runtime_check(tmp_path, now)
    assert "process-start identity" in check["message"]
    assert check["details"]["owners"][0]["missing_start_identity_pids"] == [os.getpid()]
    assert check["details"]["owners"][0]["processes_dead"] is False
    assert check["details"]["read_error"] is True
    assert check["details"]["deletion_codes"] == ["lsp_state_unreadable"]
    assert (owner / "owner.json").read_bytes() == before


def test_doctor_lsp_other_unknown_cause_does_not_claim_missing_identity() -> None:
    from llm_wiki import doctor_lsp

    check = doctor_lsp.lsp_result(
        [{"missing_start_identity_pids": []}], [], unreadable=True,
    )
    assert "process-start identity" not in check["message"]
    assert check["details"]["read_error"] is True
    assert check["details"]["deletion_codes"] == ["lsp_state_unreadable"]


def test_doctor_lsp_record_read_checks_absolute_deadline_around_handle_io(
    monkeypatch,
) -> None:
    from llm_wiki import doctor_lsp

    deadline = time.monotonic() + 10
    observed_deadlines: list[float] = []

    class Entry:
        name = "owner.json"
        kind = "file"
        file_id = b"i" * 16
        size = 2

    class Workspace:
        closed: list[int] = []

        @staticmethod
        def open_file(parent: int, name: str) -> int:
            assert (parent, name) == (7, "owner.json")
            return 8

        @staticmethod
        def identity(handle: int, *, directory: bool):
            assert (handle, directory) == (8, False)
            return 1, Entry.file_id, False

        @staticmethod
        def file_size(handle: int) -> int:
            assert handle == 8
            return 2

        @staticmethod
        def read_chunks(handle: int, *, chunk_bytes: int, max_bytes: int):
            assert (handle, chunk_bytes, max_bytes) == (8, 4096, 64 * 1024)
            yield b"{}"

        @classmethod
        def close_handle(cls, handle: int) -> None:
            cls.closed.append(handle)

    def deadline_reached(value: float) -> bool:
        observed_deadlines.append(value)
        return False

    monkeypatch.setattr(monotonic_deadline, "deadline_reached", deadline_reached)

    assert doctor_lsp.read_windows_lsp_record(Workspace, 7, Entry(), deadline) == {}
    assert observed_deadlines
    assert set(observed_deadlines) == {deadline}
    assert Workspace.closed == [8]


def test_doctor_lsp_record_disappearing_after_scan_fails_closed(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    if os.name == "nt":
        real_read = doctor_lsp.read_windows_lsp_record

        def read(opened_workspace, owner_handle, entry, deadline):
            if entry.name == "owner.json":
                raise FileNotFoundError(entry.name)
            return real_read(opened_workspace, owner_handle, entry, deadline)

        monkeypatch.setattr(doctor_lsp, "read_windows_lsp_record", read)
    else:
        real_read = doctor_lsp.read_posix_lsp_record

        def read(owner_fd, name, deadline):
            if name == "owner.json":
                raise FileNotFoundError(name)
            return real_read(owner_fd, name, deadline)

        monkeypatch.setattr(doctor_lsp, "read_posix_lsp_record", read)

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


def test_doctor_lsp_path_swap_reads_only_retained_tree(tmp_path, monkeypatch) -> None:
    import windows_workspace
    from llm_wiki import doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    state_root = tmp_path / "state"
    old_nonce = "a" * 32
    replacement_nonce = "b" * 32
    _write_lsp_owner(
        state_root,
        owner_nonce=old_nonce,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=1111,
    )
    replacement_state = tmp_path / "replacement-state"
    _write_lsp_owner(
        replacement_state,
        owner_nonce=replacement_nonce,
        generation_nonce="2" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    lsp_root = state_root / "run" / "lsp"
    replacement = replacement_state / "run" / "lsp"
    displaced = state_root / "run" / "displaced-lsp"
    swapped = False

    def swap_path() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        try:
            lsp_root.rename(displaced)
            replacement.rename(lsp_root)
        except OSError:
            # Windows retained handles may deny the rename, which is also safe.
            return

    real_scandir = os.scandir

    def scandir(path):
        if isinstance(path, int) or Path(path) == lsp_root:
            swap_path()
        return real_scandir(path)

    real_list_directory = windows_workspace.list_directory

    def list_directory(handle: int, *, max_entries: int):
        swap_path()
        return real_list_directory(handle, max_entries=max_entries)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(windows_workspace, "list_directory", list_directory)

    check = doctor_lsp.lsp_runtime_check(
        state_root,
        now,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )

    assert [owner["owner_nonce"] for owner in check["details"]["owners"]] == [old_nonce]


def test_doctor_lsp_production_live_lease_blocks_deletion(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_deletion, doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(minutes=1),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=25),
    )
    monkeypatch.setattr(
        doctor_lsp,
        "lsp_pid_state",
        lambda pid, _identity=None: "alive" if pid in {1111, 2222} else "dead",
    )

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=time.monotonic() + SHORT_TIMEOUT)
    deletion = doctor_deletion.run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert (
        check["details"]["codes"],
        deletion["quiescent"],
        deletion["permit"],
    ) == (["lsp_owner_live"], False, False)


def test_doctor_lsp_pid_probe_deadline_crossing_fails_closed(tmp_path, monkeypatch) -> None:
    import doctor
    from llm_wiki import (
        doctor_deletion,
        doctor_lsp,
    )

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(minutes=1),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=25),
    )
    clock = [1.0]
    deadline = 2.0
    probed: list[int] = []

    def pid_state(pid: int, _identity=None) -> str:
        probed.append(pid)
        clock[0] = deadline
        return "alive"

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(doctor_lsp, "lsp_pid_state", pid_state)

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=deadline)
    deletion = doctor_deletion.run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert probed == [2222]
    assert check["details"]["owners"] == []
    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert deletion["blockers"] == [{"code": "legacy_protocol_unquiesced"}]


def test_doctor_lsp_unknown_pid_probe_fails_closed(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_deletion, doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(minutes=1),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=25),
    )
    monkeypatch.setattr(
        doctor_lsp,
        "lsp_pid_state",
        lambda pid, _identity=None: "unknown",
        raising=False,
    )

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=time.monotonic() + SHORT_TIMEOUT)
    deletion = doctor_deletion.run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert "lsp_owner_live" not in check["details"]["codes"]
    assert "lsp_state_unreadable" in check["details"]["codes"]
    assert deletion["quiescent"] is False


def test_doctor_lsp_posix_eperm_pid_probe_is_unknown(monkeypatch) -> None:
    from llm_wiki import doctor_lsp

    monkeypatch.setattr(sys, "platform", "linux")

    def denied(pid: int, signal: int) -> None:
        raise PermissionError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(os, "kill", denied)

    assert doctor_lsp.lsp_pid_state(1234) == "unknown"


def test_doctor_lsp_dead_owner_uses_last_heartbeat_as_crash_evidence(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=2),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(days=1),
        expires_at=now - timedelta(days=1) + timedelta(seconds=30),
    )
    monkeypatch.setattr(doctor_lsp, "lsp_pid_state", lambda _pid, _identity=None: "dead")

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert check["details"]["codes"] == ["lsp_failure_evidence_retained"]
    assert check["details"]["owners"][0]["failure_age_days"] == pytest.approx(1)


def test_doctor_lsp_crash_evidence_expires_at_exact_seven_day_boundary(
    tmp_path, monkeypatch
) -> None:
    from llm_wiki import doctor_deletion, doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(days=7),
        expires_at=now - timedelta(days=7) + timedelta(seconds=30),
    )
    monkeypatch.setattr(doctor_lsp, "lsp_pid_state", lambda _pid, _identity=None: "dead")

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))
    deletion = doctor_deletion.run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert check["details"]["codes"] == []
    assert deletion["blockers"] == [{"code": "legacy_protocol_unquiesced"}]


@pytest.mark.parametrize(
    ("failure_age", "retained"),
    [
        (timedelta(days=7) - timedelta(seconds=1), True),
        (timedelta(days=7), False),
    ],
)
def test_doctor_lsp_failure_retention_exact_seven_day_boundary(
    tmp_path, failure_age, retained
) -> None:
    from llm_wiki import doctor_deletion, doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    _write_lsp_failure(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        timestamp=now - failure_age,
        server_pid=2222,
    )

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))
    deletion = doctor_deletion.run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert ("lsp_failure_evidence_retained" in check["details"]["codes"]) is retained
    assert deletion["quiescent"] is False


def test_doctor_lsp_failure_only_owner_without_owner_record_fails_closed(
    tmp_path,
) -> None:
    from llm_wiki import doctor_deletion, doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "a" * 32
    owner = tmp_path / "run" / "lsp" / owner_nonce
    owner.mkdir(parents=True)
    (owner / "cancellation").mkdir()
    _write_lsp_failure(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce="1" * 32,
        timestamp=now - timedelta(days=8),
        server_pid=2222,
    )

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))
    deletion = doctor_deletion.run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert deletion["blockers"] == [{"code": "legacy_protocol_unquiesced"}]


def test_doctor_lsp_dead_owner_without_lease_uses_owner_start_time(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    monkeypatch.setattr(doctor_lsp, "lsp_pid_state", lambda _pid, _identity=None: "dead")

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert check["details"]["codes"] == []
    assert check["details"]["owners"][0]["failure_age_days"] == pytest.approx(8)


def test_doctor_lsp_malformed_record_fails_closed(tmp_path) -> None:
    from llm_wiki import doctor_deletion, doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    (owner / "lease.json").write_text("{", encoding="utf-8")

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))
    deletion = doctor_deletion.run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert "lsp_state_unreadable" in check["details"]["codes"]
    assert deletion["blockers"] == [{"code": "legacy_protocol_unquiesced"}]


@pytest.mark.parametrize("duplicate_key", ["owner_nonce", "timestamp"])
def test_doctor_lsp_rejects_duplicate_json_keys(tmp_path, duplicate_key) -> None:
    from llm_wiki import doctor_deletion, doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    timestamp = _lsp_timestamp(now - timedelta(days=8))
    if duplicate_key == "owner_nonce":
        (owner / "owner.json").write_text(
            "{"
            '"command_basename":"pyright-langserver",'
            f'"generation_nonce":"{generation_nonce}",'
            f'"owner_nonce":"{owner_nonce}",'
            f'"owner_nonce":"{owner_nonce}",'
            '"owner_pid":2222,'
            f'"started_at":"{timestamp}",'
            '"state":"process_running"'
            "}",
            encoding="utf-8",
        )
    else:
        (owner / "failure.json").write_text(
            "{"
            '"code":"process_exited",'
            f'"generation_nonce":"{generation_nonce}",'
            f'"owner_nonce":"{owner_nonce}",'
            f'"timestamp":"{timestamp}",'
            f'"timestamp":"{timestamp}",'
            '"server_pid":2222'
            "}",
            encoding="utf-8",
        )

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))
    deletion = doctor_deletion.run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert (
        "lsp_state_unreadable" in check["details"]["codes"],
        deletion["quiescent"],
        deletion["permit"],
    ) == (True, False, False)


def test_doctor_lsp_deep_valid_size_json_fails_closed(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_lsp

    payload = b'{"nested":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
    assert len(payload) <= 64 * 1024

    def snapshot(state_root: Path, deadline: float):
        try:
            doctor_lsp.decode_lsp_record(payload)
        except (UnicodeError, ValueError):
            return [], True, False
        return [], False, False

    monkeypatch.setattr(doctor_lsp, "snapshot_lsp_runtime", snapshot)

    original_recursion_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(max(original_recursion_limit, 3_000))
        check = doctor_lsp.lsp_runtime_check(
            tmp_path,
            datetime(2026, 7, 31, 12, tzinfo=UTC),
            deadline=float("inf"),
        )
    finally:
        sys.setrecursionlimit(original_recursion_limit)

    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert check["details"]["read_error"] is True


def test_doctor_lsp_json_depth_ignores_delimiters_inside_strings() -> None:
    from llm_wiki import doctor_lsp

    value = '[{"escaped":"\\""}]' * 64
    payload = json.dumps({"value": value}).encode("utf-8")

    assert doctor_lsp.decode_lsp_record(payload) == {"value": value}


def test_doctor_lsp_json_depth_accepts_exact_limit() -> None:
    from llm_wiki import doctor_lsp

    array_depth = doctor_lsp.LSP_JSON_MAX_DEPTH - 1
    payload = b'{"nested":' + b"[" * array_depth + b"0" + b"]" * array_depth + b"}"

    assert isinstance(doctor_lsp.decode_lsp_record(payload), dict)


@pytest.mark.parametrize("record_name", ["owner.json", "lease.json", "failure.json"])
def test_doctor_lsp_rejects_nonproduction_record_schema(tmp_path, monkeypatch, record_name) -> None:
    from llm_wiki import doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    def write_selected_record():
        if record_name == "lease.json":
            _write_lsp_lease(
                owner,
                owner_nonce=owner_nonce,
                generation_nonce=generation_nonce,
                manager_pid=1111,
                server_pid=2222,
                heartbeat_at=now - timedelta(seconds=5),
                expires_at=now + timedelta(seconds=25),
            )
            return
        if record_name == "failure.json":
            _write_lsp_failure(
                owner,
                owner_nonce=owner_nonce,
                generation_nonce=generation_nonce,
                timestamp=now - timedelta(days=8),
                server_pid=2222,
            )
            return

    write_selected_record()
    path = owner / record_name
    record = json.loads(path.read_text(encoding="utf-8"))
    record["unexpected"] = True
    path.write_text(json.dumps(record), encoding="utf-8")

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


def test_doctor_lsp_rejects_non_integer_lease_schema_version(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(minutes=1),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=25),
    )
    lease_path = owner / "lease.json"
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
    lease["schema_version"] = 1.0
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    monkeypatch.setattr(doctor_lsp, "lsp_pid_state", lambda _pid, _identity=None: "alive")

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]
    assert "lsp_owner_live" in check["details"]["codes"]


def test_doctor_reads_a_restarted_owner_as_live(tmp_path, monkeypatch) -> None:
    """A recovery restart leaves `owner.json` naming generation 1; it is immutable.

    The lease names the generation running now, and a different nonce and
    server pid there is the normal shape of a restarted server, not a record
    to refuse. See
    `docs/research/2026-09-17-lsp-the-owner-record-names-the-first-generation-only.md`.
    """
    from llm_wiki import doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "a" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce="1" * 32,
        started_at=now - timedelta(minutes=1),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce="2" * 32,
        manager_pid=1111,
        server_pid=3333,
        heartbeat_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=25),
    )
    monkeypatch.setattr(
        doctor_lsp,
        "lsp_pid_state",
        lambda pid, _identity=None: "alive" if pid in {1111, 3333} else "dead",
    )

    codes = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))["details"][
        "codes"
    ]

    assert ("lsp_state_unreadable" in codes, "lsp_owner_live" in codes) == (False, True)
