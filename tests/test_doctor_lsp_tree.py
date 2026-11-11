"""The doctor walks the LSP runtime tree safely: owner generations, links, junctions, bounded scans and what blocks run deletion."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from llm_wiki import monotonic_deadline

from tests.doctor_case_helpers import (
    _junction_or_skip,
    _snapshot,
    _symlink_or_skip,
    _write_lsp_lease,
    give_doctor_a_generous_budget,
)
from tests.doctor_helpers import (
    _build_root,
    _qualified_pyright_check,
    _write_lsp_failure,
    _write_lsp_owner,
)

_budget_that_survives_a_slow_runner = pytest.fixture(autouse=True)(give_doctor_a_generous_budget)


def test_doctor_rejects_a_lease_belonging_to_another_owner(
    tmp_path, monkeypatch
) -> None:
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
        owner_nonce="b" * 32,
        generation_nonce="2" * 32,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=25),
    )
    monkeypatch.setattr(doctor_lsp, "lsp_pid_state", lambda _pid, _identity=None: "dead")

    codes = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))["details"][
        "codes"
    ]

    assert ("lsp_state_unreadable" in codes, "lsp_owner_live" in codes) == (True, False)


def test_doctor_accepts_failure_evidence_from_a_later_generation(tmp_path, monkeypatch) -> None:
    """The generation that failed is the one that was running, not the first."""
    from llm_wiki import doctor_lsp

    monkeypatch.setattr(doctor_lsp, "lsp_pid_state", lambda _pid, _identity=None: "dead")

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "a" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=9),
        owner_pid=2222,
    )
    _write_lsp_failure(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce="2" * 32,
        timestamp=now - timedelta(days=8),
        server_pid=3333,
    )

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" not in check["details"]["codes"]


def test_doctor_lsp_rejects_lease_heartbeat_before_owner_start(tmp_path, monkeypatch) -> None:
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
        heartbeat_at=now - timedelta(minutes=2),
        expires_at=now + timedelta(seconds=25),
    )
    monkeypatch.setattr(doctor_lsp, "lsp_pid_state", lambda _pid, _identity=None: "dead")

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]
    assert "lsp_owner_live" not in check["details"]["codes"]


def test_doctor_lsp_rejects_failure_before_owner_start(tmp_path) -> None:
    from llm_wiki import doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=1),
        owner_pid=2222,
    )
    _write_lsp_failure(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        timestamp=now - timedelta(days=2),
        server_pid=2222,
    )

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


def test_doctor_lsp_rejects_future_owner_start(tmp_path) -> None:
    from llm_wiki import doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now + timedelta(seconds=1),
        owner_pid=2222,
    )

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


def test_doctor_lsp_unknown_owner_entry_fails_closed(tmp_path) -> None:
    from llm_wiki import doctor_lsp

    unknown = tmp_path / "run" / "lsp" / "unexpected"
    unknown.mkdir(parents=True)

    check = doctor_lsp.lsp_runtime_check(
        tmp_path,
        datetime(2026, 7, 31, 12, tzinfo=UTC),
        deadline=float("inf"),
    )

    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert check["details"]["read_error"] is True


def test_doctor_lsp_129th_owner_entry_marks_scan_truncated(tmp_path) -> None:
    from llm_wiki import doctor_lsp

    lsp_root = tmp_path / "run" / "lsp"
    lsp_root.mkdir(parents=True)
    for index in range(129):
        (lsp_root / f"{index:032x}").mkdir()

    check = doctor_lsp.lsp_runtime_check(
        tmp_path,
        datetime(2026, 7, 31, 12, tzinfo=UTC),
        deadline=float("inf"),
    )

    assert len(check["details"]["owners"]) <= 128
    assert "lsp_state_unreadable" in check["details"]["codes"]
    assert check["details"]["read_error"] is True


def test_doctor_lsp_preexpired_deadline_fails_closed(tmp_path) -> None:
    from llm_wiki import doctor_lsp

    owner = tmp_path / "run" / "lsp" / ("a" * 32)
    owner.mkdir(parents=True)

    check = doctor_lsp.lsp_runtime_check(
        tmp_path,
        datetime(2026, 7, 31, 12, tzinfo=UTC),
        deadline=time.monotonic() - 1,
    )

    assert check["details"]["owners"] == []
    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert check["details"]["read_error"] is True


def test_doctor_lsp_absent_with_expired_deadline_fails_before_lstat(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_deletion, doctor_lsp

    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: (_ for _ in ()).throw(AssertionError("lstat after deadline")),
    )
    now = datetime(2026, 7, 31, 12, tzinfo=UTC)

    check = doctor_lsp.lsp_runtime_check(
        tmp_path / "absent-state",
        now,
        deadline=time.monotonic() - 1,
    )
    deletion = doctor_deletion.run_deletion_check(
        tmp_path,
        now,
        collected={
            "transactions": {"details": {"deletion_codes": []}},
            "queue": {"details": {"deletion_codes": []}},
            "archives": {"details": {"deletion_codes": []}},
            "lsp": check,
        },
    )

    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert deletion["quiescent"] is False
    assert deletion["permit"] is False


def test_doctor_posix_lsp_missing_root_rechecks_deadline(tmp_path, monkeypatch) -> None:
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    clock = [1.0]
    deadline = 2.0

    def open_root(state_root: Path, observed_deadline: float) -> int:
        assert (state_root, observed_deadline) == (tmp_path, deadline)
        clock[0] = deadline
        raise FileNotFoundError(state_root)

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(doctor_lsp, "open_posix_lsp_root", open_root)

    assert doctor_lsp.snapshot_posix_lsp(tmp_path, deadline) == ([], True, False)


def test_doctor_windows_lsp_missing_root_rechecks_deadline(tmp_path, monkeypatch) -> None:
    import doctor
    import windows_workspace
    from llm_wiki import (
        doctor_lsp,
    )

    clock = [1.0]
    deadline = 2.0

    def open_root(path: Path) -> int:
        assert path == tmp_path / "run" / "lsp"
        clock[0] = deadline
        raise FileNotFoundError(path)

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(windows_workspace, "open_directory_path", open_root)

    assert doctor_lsp.snapshot_windows_lsp(tmp_path, deadline) == ([], True, False)


def test_doctor_windows_lsp_closes_root_when_deadline_expires_after_open(
    tmp_path, monkeypatch
) -> None:
    import doctor
    import windows_workspace
    from llm_wiki import (
        doctor_lsp,
    )

    clock = [1.0]
    deadline = 2.0
    closed: list[int] = []

    def open_root(path: Path) -> int:
        assert path == tmp_path / "run" / "lsp"
        clock[0] = deadline
        return 73

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(windows_workspace, "open_directory_path", open_root)
    monkeypatch.setattr(windows_workspace, "close_handle", closed.append)

    assert doctor_lsp.snapshot_windows_lsp(tmp_path, deadline) == ([], True, False)
    assert closed == [73]


@pytest.mark.parametrize("error_type", [RuntimeError, NotImplementedError])
@pytest.mark.parametrize(
    ("failure_stage", "expected_closed"),
    [
        ("open_root", []),
        ("list_root", [10]),
        ("owner_identity", [20, 10]),
        ("cancellation_identity", [30, 20, 10]),
        ("record_read", [40, 20, 10]),
    ],
)
def test_doctor_windows_lsp_capability_failures_close_retained_handles(
    tmp_path, monkeypatch, error_type, failure_stage, expected_closed
) -> None:
    import windows_workspace as workspace
    from llm_wiki import doctor_lsp

    owner_name = "a" * 32
    owner_id = b"o" * 16
    cancellation_id = b"c" * 16
    record_id = b"r" * 16
    payload = b"{}"
    owner_entry = workspace.WindowsEntry(owner_name, "directory", owner_id)
    cancellation_entry = workspace.WindowsEntry("cancellation", "directory", cancellation_id)
    record_entry = workspace.WindowsEntry("owner.json", "file", record_id, len(payload))
    closed: list[int] = []

    def fail() -> None:
        raise error_type("Windows workspace capability unavailable")

    def fail_at(stage: str) -> None:
        if failure_stage == stage:
            fail()

    def open_root(path: Path) -> int:
        assert path == tmp_path / "run" / "lsp"
        if failure_stage == "open_root":
            fail()
        return 10

    def list_directory(handle: int, *, max_entries: int):
        if handle == 10:
            fail_at("list_root")
            return [owner_entry]
        assert (handle, max_entries) == (20, len(doctor_lsp.LSP_OWNER_ENTRY_NAMES))
        if failure_stage == "cancellation_identity":
            return [cancellation_entry]
        return [record_entry]

    def open_directory(parent: int, name: str) -> int:
        if parent == 10:
            assert name == owner_name
            return 20
        assert (parent, name) == (20, "cancellation")
        return 30

    def identity(handle: int, *, directory: bool | None = None):
        if handle == 20:
            fail_at("owner_identity")
            return 1, owner_id, True
        if handle == 30:
            fail_at("cancellation_identity")
            return 1, cancellation_id, True
        assert (handle, directory) == (40, False)
        return 1, record_id, False

    def read_chunks(handle: int, *, chunk_bytes: int, max_bytes: int):
        assert handle == 40
        if failure_stage == "record_read":
            fail()
        return [payload]

    monkeypatch.setattr(workspace, "open_directory_path", open_root)
    monkeypatch.setattr(workspace, "list_directory", list_directory)
    monkeypatch.setattr(workspace, "open_directory", open_directory)
    monkeypatch.setattr(workspace, "identity", identity)
    monkeypatch.setattr(workspace, "open_file", lambda parent, name: 40)
    monkeypatch.setattr(workspace, "file_size", lambda handle: len(payload))
    monkeypatch.setattr(workspace, "read_chunks", read_chunks)
    monkeypatch.setattr(workspace, "close_handle", closed.append)

    _snapshots, unreadable, absent = doctor_lsp.snapshot_windows_lsp(tmp_path, float("inf"))

    assert unreadable is True
    assert absent is False
    assert closed == expected_closed


def test_doctor_lsp_deadline_expiring_during_scan_fails_closed(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_lsp

    owner = tmp_path / "run" / "lsp" / ("a" * 32)
    owner.mkdir(parents=True)
    observations = iter((False, True))
    monkeypatch.setattr(
        monotonic_deadline,
        "deadline_reached",
        lambda deadline: next(observations, True),
    )

    check = doctor_lsp.lsp_runtime_check(
        tmp_path,
        datetime(2026, 7, 31, 12, tzinfo=UTC),
        deadline=1.0,
    )

    assert check["details"]["owners"] == []
    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert check["details"]["read_error"] is True


@pytest.mark.parametrize("link_kind", ["symlink", "junction"])
def test_doctor_lsp_linked_root_fails_closed(tmp_path, link_kind) -> None:
    from llm_wiki import doctor_lsp

    external = tmp_path / "external"
    external.mkdir()
    lsp_root = tmp_path / "run" / "lsp"
    lsp_root.parent.mkdir(parents=True)
    if link_kind == "symlink":
        _symlink_or_skip(lsp_root, external, directory=True)
    else:
        _junction_or_skip(lsp_root, external)

    check = doctor_lsp.lsp_runtime_check(
        tmp_path,
        datetime(2026, 7, 31, 12, tzinfo=UTC),
        deadline=float("inf"),
    )

    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert check["details"]["read_error"] is True


def test_doctor_lsp_external_run_junction_fails_closed(tmp_path) -> None:
    from llm_wiki import doctor_deletion, doctor_lsp

    state_root = tmp_path / "state"
    state_root.mkdir()
    external_run = tmp_path / "external-run"
    (external_run / "lsp").mkdir(parents=True)
    run_root = state_root / "run"
    if os.name == "nt":
        _junction_or_skip(run_root, external_run)
    else:
        _symlink_or_skip(run_root, external_run, directory=True)

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    check = doctor_lsp.lsp_runtime_check(state_root, now, deadline=float("inf"))
    deletion = doctor_deletion.run_deletion_check(state_root, now, collected={"lsp": check})

    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert deletion["blockers"] == [{"code": "legacy_protocol_unquiesced"}]


@pytest.mark.parametrize("link_kind", ["symlink", "junction"])
def test_doctor_lsp_linked_owner_fails_closed(tmp_path, link_kind) -> None:
    from llm_wiki import doctor_lsp

    external = tmp_path / "external-owner"
    external.mkdir()
    lsp_root = tmp_path / "run" / "lsp"
    lsp_root.mkdir(parents=True)
    owner = lsp_root / ("a" * 32)
    if link_kind == "symlink":
        _symlink_or_skip(owner, external, directory=True)
    else:
        _junction_or_skip(owner, external)

    check = doctor_lsp.lsp_runtime_check(
        tmp_path,
        datetime(2026, 7, 31, 12, tzinfo=UTC),
        deadline=float("inf"),
    )

    assert "lsp_state_unreadable" in check["details"]["codes"]
    assert check["details"]["read_error"] is True


def test_doctor_lsp_symlink_record_entry_fails_closed(tmp_path) -> None:
    from llm_wiki import doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    external = tmp_path / "external-lease.json"
    external.write_text("{}", encoding="utf-8")
    _symlink_or_skip(owner / "lease.json", external)

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


@pytest.mark.parametrize("link_kind", ["symlink", "junction"])
def test_doctor_lsp_linked_cancellation_directory_fails_closed(tmp_path, link_kind) -> None:
    from llm_wiki import doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    cancellation = owner / "cancellation"
    cancellation.rmdir()
    external = tmp_path / "external-cancellation"
    external.mkdir()
    if link_kind == "symlink":
        _symlink_or_skip(cancellation, external, directory=True)
    else:
        _junction_or_skip(cancellation, external)

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


def _bound_owner_listing(monkeypatch, scandir, observed: list) -> None:
    """Watch the listing the owner scan uses, by whichever call this platform makes."""
    if os.name != "nt":
        monkeypatch.setattr(os, "scandir", scandir)
        return
    import windows_workspace

    real_list_directory = windows_workspace.list_directory

    def list_directory(handle: int, *, max_entries: int):
        observed.append(max_entries)
        return real_list_directory(handle, max_entries=max_entries)

    monkeypatch.setattr(windows_workspace, "list_directory", list_directory)


def _windows_limit_observed(observed: list) -> bool:
    """Windows passes the bound to the listing call; POSIX has no such argument."""
    if os.name != "nt":
        return True
    return 4 in observed


def _is_owner_handle(path, owner) -> bool:
    """Whether this scandir argument is the open descriptor of the owner directory."""
    if not isinstance(path, int):
        return False
    return os.path.samestat(os.fstat(path), owner.stat())


def test_doctor_lsp_owner_child_scan_stops_at_fifth_entry(tmp_path, monkeypatch) -> None:
    from llm_wiki import (
        doctor_lsp,
    )

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    for index in range(5):
        (owner / f"unexpected-{index}").write_text("", encoding="utf-8")
    real_scandir = os.scandir
    observed_windows_limits: list[int] = []

    class BoundedScan:
        def __init__(self, path) -> None:
            self.scanned = real_scandir(path)
            self.count = 0

        def __enter__(self):
            self.scanned.__enter__()
            return self

        def __exit__(self, *args):
            return self.scanned.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            self.count += 1
            if self.count > 5:
                raise AssertionError("owner child scan consumed a sixth entry")
            return next(self.scanned)

    def scandir(path):
        if _is_owner_handle(path, owner):
            return BoundedScan(path)
        return real_scandir(path)

    _bound_owner_listing(monkeypatch, scandir, observed_windows_limits)

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert (
        "lsp_state_unreadable" in check["details"]["codes"],
        _windows_limit_observed(observed_windows_limits),
    ) == (True, True)


def test_doctor_lsp_unknown_owner_child_fails_closed(tmp_path) -> None:
    from llm_wiki import doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    (owner / "unexpected.json").write_text("{}", encoding="utf-8")

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


def test_doctor_lsp_live_owner_and_failure_block_deletion(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_deletion, doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    live_owner_nonce = "a" * 32
    live_generation_nonce = "1" * 32
    live_owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=live_owner_nonce,
        generation_nonce=live_generation_nonce,
        started_at=now - timedelta(minutes=1),
        owner_pid=2222,
    )
    _write_lsp_lease(
        live_owner,
        owner_nonce=live_owner_nonce,
        generation_nonce=live_generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=25),
    )
    failure_owner_nonce = "b" * 32
    failure_generation_nonce = "2" * 32
    failure_owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=failure_owner_nonce,
        generation_nonce=failure_generation_nonce,
        started_at=now - timedelta(days=2),
        owner_pid=3333,
    )
    _write_lsp_failure(
        failure_owner,
        owner_nonce=failure_owner_nonce,
        generation_nonce=failure_generation_nonce,
        timestamp=now - timedelta(days=1),
        server_pid=3333,
    )
    monkeypatch.setattr(
        doctor_lsp,
        "lsp_pid_state",
        lambda pid, _identity=None: "alive" if pid in {1111, 2222} else "dead",
    )

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))
    deletion = doctor_deletion.run_deletion_check(tmp_path, now, collected={"lsp": check})
    blocker_codes = {item["code"] for item in deletion["blockers"]}
    assert blocker_codes == {"legacy_protocol_unquiesced"}
    assert deletion["quiescent"] is False


def test_doctor_lsp_expired_failure_does_not_block(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_deletion, doctor_lsp

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "c" * 32
    generation_nonce = "3" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=9),
        owner_pid=3333,
    )
    _write_lsp_failure(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        timestamp=now - timedelta(days=8),
        server_pid=3333,
    )

    check = doctor_lsp.lsp_runtime_check(tmp_path, now, deadline=float("inf"))
    deletion = doctor_deletion.run_deletion_check(tmp_path, now, collected={"lsp": check})
    assert "lsp_failure_evidence_retained" not in {item["code"] for item in deletion["blockers"]}


def test_doctor_repair_preserves_lsp_runtime_bytes(tmp_path, monkeypatch) -> None:
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        state_root,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=2),
        owner_pid=2222,
    )
    _write_lsp_failure(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        timestamp=now - timedelta(days=1),
        server_pid=2222,
    )
    lsp_root = state_root / "run" / "lsp"
    before = _snapshot(lsp_root)
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)

    doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        repair=True,
        repair_actions={"runtime"},
        now=now,
        time_budget_seconds=30,
    )

    assert _snapshot(lsp_root) == before
