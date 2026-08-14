from __future__ import annotations

import contextlib
import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import markdown_transaction
import operational_ownership as ownership
import pytest
from reliable_memory import capture_runtime_file_identity, sha256_bytes

ALL_ROLES = (
    "capture",
    "project",
    "markdown-writer",
    "queue-worker",
    "compile",
    "doctor",
    "nightly",
    "weekly",
    "lsp",
    "queue-operator",
    "repair",
    "runtime-deletion-check",
)
MARKER_ROLES = {"compile", "nightly", "weekly"}
LONG_ROLES = {
    "queue-worker",
    "compile",
    "nightly",
    "weekly",
    "queue-operator",
    "repair",
}


@dataclass
class _Clock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def _candidate(state_root: Path) -> Path:
    return state_root / "run" / "markdown-transactions-v3.candidate.sqlite3"


def _initialize_candidate(state_root: Path) -> Path:
    path = _candidate(state_root)
    markdown_transaction.initialize_coordinator_v3_candidate(path, source_v2=None)
    return path


def _registry(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock: _Clock | None = None,
    process_probe=None,
) -> tuple[ownership.OwnershipRegistry, _Clock]:
    _initialize_candidate(state_root)
    current = ownership.ProcessIdentity(pid=31001, start_identity="test-process:1")
    monkeypatch.setattr(ownership, "current_process_identity", lambda: current)
    selected_clock = clock or _Clock(datetime(2026, 8, 12, 12, tzinfo=timezone.utc))
    kwargs = {"clock": selected_clock}
    if process_probe is not None:
        kwargs["process_probe"] = process_probe
    return ownership.OwnershipRegistry(state_root, **kwargs), selected_clock


def _marker(state_root: Path, role: str) -> ownership.MarkerIdentity:
    relative = "run/compile.pid" if role == "compile" else "run/maintenance.lock"
    path = state_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"31001\n"
    path.write_bytes(payload)
    return ownership.MarkerIdentity(
        relative_path=relative,
        sha256=sha256_bytes(payload),
        file_identity=capture_runtime_file_identity(path, state_root=state_root),
        pid=31001,
    )


def _acquire(
    registry: ownership.OwnershipRegistry,
    state_root: Path,
    role: str,
    *,
    actor_id: str,
    token: str,
) -> ownership.OwnerLease:
    marker = _marker(state_root, role) if role in MARKER_ROLES else None
    return registry.acquire(
        role,
        scope="global",
        actor_id=actor_id,
        token=token,
        marker=marker,
    )


def _owner_count(state_root: Path) -> int:
    with contextlib.closing(sqlite3.connect(_candidate(state_root))) as database:
        return int(database.execute("SELECT COUNT(*) FROM maintenance_owners").fetchone()[0])


def _expire(state_root: Path, lease: ownership.OwnerLease, when: datetime) -> None:
    with contextlib.closing(sqlite3.connect(_candidate(state_root))) as database:
        database.execute(
            "UPDATE maintenance_owners SET expires_at=? WHERE role=? AND scope=?",
            (when.isoformat().replace("+00:00", "Z"), lease.role, lease.scope),
        )
        database.commit()


def test_closed_roles_use_only_the_approved_lease_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    registry, _clock = _registry(state_root, monkeypatch)

    for index, role in enumerate(ALL_ROLES):
        lease = _acquire(
            registry,
            state_root,
            role,
            actor_id=f"actor-{index}",
            token=f"token-{index}",
        )
        expected = (120, 40) if role in LONG_ROLES else (30, 10)
        assert (lease.ttl_seconds, lease.heartbeat_seconds) == expected
        registry.release(lease)

    with pytest.raises(ValueError, match="role"):
        registry.acquire("backup", scope="global")  # type: ignore[arg-type]


@pytest.mark.parametrize("expired", [False, True], ids=("live-lease", "expired"))
@pytest.mark.parametrize("process_state", ["alive", "dead", "unknown"])
def test_takeover_requires_expiry_and_positive_process_death(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expired: bool,
    process_state: str,
) -> None:
    state_root = tmp_path / "state"
    registry, clock = _registry(
        state_root, monkeypatch, process_probe=lambda _identity: process_state
    )
    first = registry.acquire(
        "doctor", scope="global", actor_id="first-actor", token="first-token"
    )
    _expire(
        state_root,
        first,
        clock.value - timedelta(seconds=1)
        if expired
        else clock.value + timedelta(seconds=1),
    )

    if expired and process_state == "dead":
        second = registry.acquire(
            "doctor", scope="global", actor_id="second-actor", token="second-token"
        )
        assert second.epoch == first.epoch + 1
        assert second.token == "second-token"
        return

    with pytest.raises(ownership.OperationalOwnershipError) as error:
        registry.acquire(
            "doctor", scope="global", actor_id="second-actor", token="second-token"
        )
    expected = "owner_liveness_unknown" if expired and process_state == "unknown" else "owner_busy"
    assert error.value.code == expected
    assert _owner_count(state_root) == 1


@pytest.mark.parametrize(
    ("observed", "expected"),
    [("linux:boot:10", "alive"), ("linux:boot:11", "dead")],
)
def test_pid_reuse_is_dead_only_when_start_identity_differs(
    monkeypatch: pytest.MonkeyPatch, observed: str, expected: str
) -> None:
    identity = ownership.ProcessIdentity(pid=99, start_identity="linux:boot:10")
    monkeypatch.setattr(ownership, "process_start_identity", lambda _pid: observed)

    assert ownership.process_identity_state(identity) == expected


def test_denied_liveness_is_unknown_and_blocks_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    registry, clock = _registry(state_root, monkeypatch)
    first = registry.acquire(
        "doctor", scope="global", actor_id="first-actor", token="first-token"
    )
    _expire(state_root, first, clock.value - timedelta(seconds=1))

    def denied(_pid: int) -> str | None:
        raise PermissionError("simulated access denial")

    monkeypatch.setattr(ownership, "process_start_identity", denied)
    with pytest.raises(ownership.OperationalOwnershipError) as error:
        registry.acquire(
            "doctor", scope="global", actor_id="second-actor", token="second-token"
        )

    assert ownership.process_identity_state(first.process) == "unknown"
    assert error.value.code == "owner_liveness_unknown"
    assert _owner_count(state_root) == 1


@pytest.mark.parametrize("method", ["heartbeat", "release"])
@pytest.mark.parametrize(
    "change",
    ["token", "epoch", "pid", "start_identity"],
)
def test_heartbeat_and_release_verify_one_affected_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    change: str,
) -> None:
    state_root = tmp_path / "state"
    registry, _clock = _registry(state_root, monkeypatch)
    lease = registry.acquire(
        "doctor", scope="global", actor_id="actor", token="exact-token"
    )
    if change == "token":
        stale = replace(lease, token="stale-token")
    elif change == "epoch":
        stale = replace(lease, epoch=lease.epoch + 1)
    elif change == "pid":
        stale = replace(lease, process=replace(lease.process, pid=lease.process.pid + 1))
    else:
        stale = replace(
            lease,
            process=replace(lease.process, start_identity="different-process"),
        )

    with pytest.raises(ownership.OperationalOwnershipError) as error:
        getattr(registry, method)(stale)

    assert error.value.code == "owner_fence_lost"
    assert _owner_count(state_root) == 1
    registry.release(lease)


@pytest.mark.parametrize("method", ["heartbeat", "release"])
def test_marker_owner_checks_exact_fence_before_marker_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    state_root = tmp_path / "state"
    registry, _clock = _registry(state_root, monkeypatch)
    marker = _marker(state_root, "compile")
    lease = registry.acquire(
        "compile",
        scope="global",
        actor_id="compile-actor",
        token="compile-token",
        marker=marker,
    )
    stale = replace(lease, process=replace(lease.process, pid=lease.process.pid + 1))

    with pytest.raises(ownership.OperationalOwnershipError) as error:
        getattr(registry, method)(stale)

    assert error.value.code == "owner_fence_lost"
    assert _owner_count(state_root) == 1
    registry.release(lease)


@pytest.mark.parametrize("role", [role for role in ALL_ROLES if role != "runtime-deletion-check"])
def test_runtime_deletion_check_requires_zero_other_canonical_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    state_root = tmp_path / "state"
    registry, _clock = _registry(state_root, monkeypatch)
    owner = _acquire(
        registry, state_root, role, actor_id="other-actor", token="other-token"
    )

    with pytest.raises(ownership.OperationalOwnershipError) as error:
        registry.acquire(
            "runtime-deletion-check",
            scope="global",
            actor_id="snapshot-actor",
            token="snapshot-token",
        )

    assert error.value.code == "runtime_deletion_check_requires_quiescence"
    assert _owner_count(state_root) == 1
    registry.release(owner)


def test_every_other_acquisition_rejects_runtime_deletion_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    registry, _clock = _registry(state_root, monkeypatch)
    snapshot = registry.acquire(
        "runtime-deletion-check",
        scope="global",
        actor_id="snapshot-actor",
        token="snapshot-token",
    )

    for index, role in enumerate(role for role in ALL_ROLES if role != snapshot.role):
        marker = _marker(state_root, role) if role in MARKER_ROLES else None
        with pytest.raises(ownership.OperationalOwnershipError) as error:
            registry.acquire(
                role,
                scope="global",
                actor_id=f"blocked-actor-{index}",
                token=f"blocked-token-{index}",
                marker=marker,
            )
        assert error.value.code == "runtime_deletion_check_active"
        assert _owner_count(state_root) == 1

    registry.release(snapshot)


def test_heartbeat_require_release_and_successor_use_exact_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    registry, clock = _registry(state_root, monkeypatch)
    first = registry.acquire(
        "doctor", scope="global", actor_id="first-actor", token="first-token"
    )
    clock.value += timedelta(seconds=5)
    renewed = registry.heartbeat(first)
    assert renewed.heartbeat_at == clock.value
    assert renewed.expires_at == clock.value + timedelta(seconds=30)

    with contextlib.closing(sqlite3.connect(_candidate(state_root))) as database:
        registry.require(database, renewed)

    registry.release(renewed)
    successor = registry.acquire(
        "doctor", scope="global", actor_id="second-actor", token="second-token"
    )
    assert successor.epoch == renewed.epoch + 1


def test_marker_identity_change_blocks_release_and_preserves_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    registry, _clock = _registry(state_root, monkeypatch)
    marker = _marker(state_root, "compile")
    lease = registry.acquire(
        "compile",
        scope="global",
        actor_id="compile-actor",
        token="compile-token",
        marker=marker,
    )
    (state_root / marker.relative_path).write_bytes(b"changed\n")

    with pytest.raises(ownership.OperationalOwnershipError) as error:
        registry.release(lease)

    assert error.value.code == "marker_identity_invalid"
    assert _owner_count(state_root) == 1


def test_process_identity_dispatch_liveness_and_actor_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ownership, "_platform_system", lambda: "Linux")
    monkeypatch.setattr(
        ownership, "_linux_process_start_identity", lambda pid: f"linux:boot:{pid}"
    )
    monkeypatch.setattr(ownership.os, "getpid", lambda: 77)
    monkeypatch.setattr(ownership.os, "getuid", lambda: 501, raising=False)

    identity = ownership.current_process_identity()

    assert identity == ownership.ProcessIdentity(pid=77, start_identity="linux:boot:77")
    assert ownership.process_identity_state(identity) == "alive"
    assert ownership.current_actor_identity() == "posix-uid:501"

    monkeypatch.setattr(ownership, "_platform_system", lambda: "Darwin")
    monkeypatch.setattr(
        ownership, "_darwin_process_start_identity", lambda pid: f"darwin:1:{pid}"
    )
    assert ownership.process_start_identity(77) == "darwin:1:77"

    monkeypatch.setattr(ownership, "_platform_system", lambda: "Windows")
    monkeypatch.setattr(
        ownership, "_windows_process_start_identity", lambda pid: f"windows:{pid}"
    )
    monkeypatch.setattr(ownership, "_windows_actor_identity", lambda: "windows-sid:S-1-5-21")
    assert ownership.process_start_identity(77) == "windows:77"
    assert ownership.current_actor_identity() == "windows-sid:S-1-5-21"


def test_current_platform_actor_identity_is_bounded_and_namespaced() -> None:
    actor = ownership.current_actor_identity()

    assert actor.startswith(("posix-uid:", "windows-sid:"))
    assert len(actor.encode("utf-8")) <= 256


def test_linux_identity_binds_boot_id_and_start_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stat = b"42 (worker name) S " + b" ".join(str(value).encode() for value in range(1, 23))

    def read(path: Path, _maximum: int) -> bytes:
        if path == Path("/proc/42/stat"):
            return stat
        return b"550e8400-e29b-41d4-a716-446655440000\n"

    monkeypatch.setattr(ownership, "_read_bounded_system_file", read)

    assert ownership._linux_process_start_identity(42) == (
        "linux:550e8400-e29b-41d4-a716-446655440000:19"
    )


def test_registry_mutates_only_the_v3_candidate_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    registry, _clock = _registry(state_root, monkeypatch)
    legacy = state_root / "run" / "markdown-transactions.sqlite3"
    assert not legacy.exists()

    lease = registry.acquire(
        "doctor", scope="global", actor_id="actor", token="token"
    )

    assert _candidate(state_root).is_file()
    assert not legacy.exists()
    assert not (state_root / "run" / "markdown-transactions-v3.sqlite3").exists()
    registry.release(lease)


def test_scheduled_owner_heartbeat_covers_the_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    _initialize_candidate(state_root)
    lease, marker = ownership.acquire_scheduled_owner(
        "weekly", state_root=state_root
    )
    calls: list[ownership.OwnerLease] = []
    wake = threading.Event()
    real_heartbeat = ownership.OwnershipRegistry.heartbeat

    def heartbeat(current_registry, current_lease):
        renewed = real_heartbeat(current_registry, current_lease)
        calls.append(renewed)
        wake.set()
        return renewed

    monkeypatch.setattr(ownership.OwnershipRegistry, "heartbeat", heartbeat)
    waits = iter((False, True))
    monkeypatch.setattr(
        ownership,
        "_wait_for_owner_heartbeat",
        lambda _stop, _seconds: next(waits, True),
    )
    monkeypatch.setattr(ownership, "_join_owner_heartbeat", lambda thread, _timeout: thread.join())

    with ownership.heartbeat_owner(lease):
        assert wake.wait(2)

    assert calls[0].heartbeat_at >= lease.heartbeat_at
    ownership.release_marker_owner(calls[-1], marker)


def test_marker_release_clears_canonical_before_exact_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    _initialize_candidate(state_root)
    lease, marker = ownership.acquire_scheduled_owner(
        "weekly", state_root=state_root
    )
    observed: list[int] = []
    real_remove = ownership._remove_exact_marker

    def observe_remove(root: Path, current: ownership.MarkerIdentity) -> None:
        with contextlib.closing(sqlite3.connect(_candidate(state_root))) as database:
            observed.append(
                database.execute(
                    "SELECT COUNT(*) FROM maintenance_owners WHERE owner_token=?",
                    (lease.token,),
                ).fetchone()[0]
            )
        real_remove(root, current)

    monkeypatch.setattr(ownership, "_remove_exact_marker", observe_remove)
    ownership.release_marker_owner(lease, marker)

    assert observed == [0]
