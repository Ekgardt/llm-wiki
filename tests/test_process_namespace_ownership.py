"""A missing PID in another namespace is not proof that its owner died."""
from __future__ import annotations

import io
import os
from pathlib import Path
from types import SimpleNamespace

import operational_ownership as ownership
import process_liveness as liveness
import pytest

from tests.slow_machine import SHORT_TIMEOUT

BOOT = "550e8400-e29b-41d4-a716-446655440000"
HOST = "1" * 64
SCOPE = (HOST, BOOT, "4", "100", "4.200")


def _identity(scope=SCOPE, ticks="10"):
    return ":".join(("linux-v2", *scope, ticks))


def _observe(monkeypatch, observed):
    monkeypatch.setattr(liveness, "_platform_system", lambda: "Linux")
    monkeypatch.setattr(liveness, "_linux_observer_scope", lambda: SCOPE, raising=False)
    monkeypatch.setattr(liveness, "process_start_identity", lambda _pid: observed)


@pytest.mark.parametrize("observed", [None, _identity(), _identity(ticks="11")])
@pytest.mark.parametrize("recorded", [
    _identity((HOST, BOOT, "4", "101", "4.200")),
    _identity((HOST, BOOT, "4", "100", "4.201")),
    f"linux:{BOOT}:10",
    "unknown-format",
])
def test_invisible_or_unscoped_owner_never_becomes_dead(monkeypatch, observed, recorded):
    _observe(monkeypatch, observed)
    process = ownership.ProcessIdentity(99, recorded)
    assert ownership.process_identity_state(process) == "unknown"
    assert liveness.owner_alive(99, recorded)


@pytest.mark.parametrize(("observed", "expected"), [
    (_identity(), "alive"), (_identity(ticks="11"), "dead"), (None, "dead"),
])
def test_same_scope_preserves_exit_and_pid_reuse_detection(monkeypatch, observed, expected):
    _observe(monkeypatch, observed)
    assert ownership.process_identity_state(ownership.ProcessIdentity(99, _identity())) == expected
    assert liveness.owner_alive(99, _identity()) is (expected != "dead")


def test_linux_pid_only_record_cannot_prove_death(monkeypatch):
    _observe(monkeypatch, None)
    assert liveness.owner_alive(99)


@pytest.mark.parametrize(("scope", "expected"), [
    ((HOST, "650e8400-e29b-41d4-a716-446655440000", "4", "100", "4.200"), "dead"),
    (("2" * 64, BOOT, "4", "100", "4.200"), "unknown"),
    (("-", "650e8400-e29b-41d4-a716-446655440000", "4", "100", "4.200"), "unknown"),
])
def test_reboot_requires_the_same_verified_host(monkeypatch, scope, expected):
    _observe(monkeypatch, None)
    assert ownership.process_identity_state(ownership.ProcessIdentity(99, _identity(scope))) == expected


def test_an_unreadable_scope_is_unknown_even_when_the_pid_is_missing(monkeypatch):
    _observe(monkeypatch, None)

    def denied():
        raise PermissionError("namespace is not inspectable")

    monkeypatch.setattr(liveness, "_linux_observer_scope", denied)
    assert ownership.process_identity_state(ownership.ProcessIdentity(99, _identity())) == "unknown"


def test_every_persisted_owner_consumer_preserves_unknown(monkeypatch, tmp_path):
    import doctor
    import install_pyright
    import installed_memory_repair
    import lsp_process
    import markdown_transaction

    _observe(monkeypatch, None)
    hidden = _identity((HOST, BOOT, "4", "101", "4.200"))
    assert doctor._owner_pid_live(99, hidden)
    assert doctor._owner_pid_live(99)
    assert markdown_transaction._preparer_alive(tmp_path, 99)
    assert install_pyright._lock_owner_is_alive({"pid": 99, "process_start": hidden})
    assert not lsp_process._process_is_gone(99, hidden)
    assert not lsp_process._process_is_gone(99, None)
    with pytest.raises(ownership.OperationalOwnershipError, match="owner_liveness_unknown"):
        ownership._marker_owner_alive(99, hidden)
    with pytest.raises(ValueError, match="unknown legacy owner"):
        installed_memory_repair._require_process_absent(99, hidden)


@pytest.mark.parametrize("raw", [b"", b"0" * 32, b"not-a-machine-id", b"g" * 32])
def test_an_invalid_machine_id_is_explicitly_unavailable(monkeypatch, raw):
    monkeypatch.setattr(liveness, "_read_bounded_system_file", lambda _path, _bound: raw)
    assert liveness._linux_machine_key() == "-"


def test_an_unreadable_machine_id_does_not_prevent_same_boot_operation(monkeypatch):
    def denied(_path, _bound):
        raise PermissionError("machine ID unavailable")

    monkeypatch.setattr(liveness, "_read_bounded_system_file", denied)
    assert liveness._linux_machine_key() == "-"
    scope = ("-", BOOT, "4", "100", "4.200")
    _observe(monkeypatch, _identity(scope))
    monkeypatch.setattr(liveness, "_linux_observer_scope", lambda: scope)
    assert liveness.owner_alive(99, _identity(scope))


def test_machine_key_is_application_specific_and_does_not_expose_the_machine_id(monkeypatch):
    import hmac

    raw = b"0123456789abcdef" * 2
    monkeypatch.setattr(liveness, "_read_bounded_system_file", lambda _path, _bound: raw + b"\n")
    actual = liveness._linux_machine_key()
    assert actual == hmac.digest(b"LLM Wiki Linux ownership host v2", raw, "sha256").hex()
    assert raw.decode() not in actual


def _procfs_status(monkeypatch, payload):
    original = Path.open

    def opened(path, *args, **kwargs):
        if path == Path("/proc/self/status"):
            return io.BytesIO(payload)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)


@pytest.mark.parametrize("values", [b"123 99", b"99 99", b"", b"123", b"invalid"])
def test_foreign_procfs_coordinates_are_refused_before_probing_a_pid(monkeypatch, values):
    monkeypatch.setattr(os, "getpid", lambda: 99)
    _procfs_status(monkeypatch, b"NStgid:\t" + values + b"\n")
    monkeypatch.setattr(Path, "stat", lambda *_args, **_kwargs: SimpleNamespace(st_dev=4, st_ino=100))

    with pytest.raises(liveness.ProcessIdentityUnavailable, match="different PID namespace"):
        liveness._linux_namespace_scope()


def test_namespace_scope_does_not_need_access_to_pid_one(monkeypatch):
    def namespace(path, **_kwargs):
        if path == Path("/proc/1/ns/pid"):
            raise PermissionError("PID 1 belongs to another user")
        return SimpleNamespace(st_dev=4, st_ino=100)

    _procfs_status(monkeypatch, f"NStgid:\t{os.getpid()}\n".encode())
    monkeypatch.setattr(Path, "stat", namespace)
    assert liveness._linux_namespace_scope() == ("4", "100")


def test_missing_namespace_coordinates_are_not_guessed(monkeypatch):
    _procfs_status(monkeypatch, b"Name:\tpython\nPid:\t99\n")
    monkeypatch.setattr(Path, "stat", lambda *_args, **_kwargs: SimpleNamespace(st_dev=4, st_ino=100))
    with pytest.raises(liveness.ProcessIdentityUnavailable):
        liveness._linux_namespace_scope()


def test_an_expired_hidden_owner_blocks_registry_takeover(monkeypatch, tmp_path):
    from datetime import timedelta

    from tests.test_operational_ownership import _expire, _owner_count, _registry

    registry, clock = _registry(tmp_path, monkeypatch)
    _observe(monkeypatch, None)
    hidden = ownership.ProcessIdentity(99, _identity((HOST, BOOT, "4", "101", "4.200")))
    monkeypatch.setattr(ownership, "current_process_identity", lambda: hidden)
    first = registry.acquire("doctor", scope="global", actor_id="first", token="first")
    _expire(tmp_path, first, clock.value - timedelta(seconds=1))
    current = ownership.ProcessIdentity(100, _identity())
    monkeypatch.setattr(ownership, "current_process_identity", lambda: current)
    with pytest.raises(ownership.OperationalOwnershipError, match="owner_liveness_unknown"):
        registry.acquire("doctor", scope="global", actor_id="second", token="second")
    assert _owner_count(tmp_path) == 1


def test_time_namespace_changes_the_process_identity(monkeypatch):
    stat = b"99 (worker) S " + b" ".join(str(value).encode() for value in range(1, 23))
    monkeypatch.setattr(liveness, "_read_bounded_system_file", lambda _path, _size: stat)
    monkeypatch.setattr(liveness, "_linux_boot_id", lambda: BOOT)
    monkeypatch.setattr(liveness, "_linux_machine_key", lambda: HOST)
    monkeypatch.setattr(liveness, "_linux_namespace_scope", lambda: ("4", "100"))
    monkeypatch.setattr(Path, "stat", lambda *_args, **_kwargs: SimpleNamespace(st_dev=4, st_ino=200))
    first = liveness._linux_process_start_identity(99)
    monkeypatch.setattr(Path, "stat", lambda *_args, **_kwargs: SimpleNamespace(st_dev=4, st_ino=201))
    second = liveness._linux_process_start_identity(99)
    assert first != second


def test_doctor_accepts_the_complete_scoped_identity():
    import doctor

    identity = _identity((HOST, BOOT, "4", "4026532530", "4.4026531834"), ticks="135592828")
    assert doctor._lsp_start_identity(identity)


def test_a_kernel_without_time_namespaces_records_absence(monkeypatch):
    def absent(*_args, **_kwargs):
        raise FileNotFoundError("no time namespace handle")

    monkeypatch.setattr(Path, "stat", absent)
    assert liveness._linux_time_scope() == "none"


def test_an_inaccessible_time_namespace_is_not_an_absent_feature(monkeypatch):
    def denied(*_args, **_kwargs):
        raise PermissionError("time namespace handle is inaccessible")

    monkeypatch.setattr(Path, "stat", denied)
    with pytest.raises(PermissionError):
        liveness._linux_time_scope()


def _lsp_records(identity):
    owner = {
        "command_basename": "pyright-langserver", "generation_nonce": "b" * 32,
        "state": "process_running",
        "owner_nonce": "a" * 32, "owner_pid": 99,
        "owner_start_identity": identity, "started_at": "2026-09-29T12:00:00Z",
    }
    lease = {
        "schema_version": 1, "generation_nonce": "b" * 32, "state": "live",
        "owner_nonce": "a" * 32, "manager_pid": 99, "server_pid": 100,
        "manager_start_identity": identity, "server_start_identity": identity,
        "heartbeat_at": "2026-09-29T12:00:01Z",
        "expires_at": "2026-09-29T12:00:02Z",
    }
    return owner, lease


@pytest.mark.parametrize("with_lease", [True, False])
def test_doctor_never_uses_lease_expiry_as_process_death(monkeypatch, with_lease):
    import time
    from datetime import datetime, timezone

    import doctor

    _observe(monkeypatch, _identity())
    owner, lease = _lsp_records(_identity())
    if not with_lease:
        lease = None
    now = datetime(2026, 9, 29, 12, 0, 3, tzinfo=timezone.utc)
    result = doctor._lsp_liveness(owner, lease, "a" * 32, now, time.monotonic() + SHORT_TIMEOUT)
    assert result.live


def test_doctor_keeps_hidden_lsp_owners_unknown_after_expiry(monkeypatch):
    import time
    from datetime import datetime, timezone

    import doctor

    _observe(monkeypatch, None)
    owner, lease = _lsp_records(_identity((HOST, BOOT, "4", "101", "4.200")))
    now = datetime(2026, 9, 29, 12, 0, 3, tzinfo=timezone.utc)
    result = doctor._lsp_liveness(owner, lease, "a" * 32, now, time.monotonic() + SHORT_TIMEOUT)
    assert result.unreadable


def test_failure_retirement_requires_positive_death_evidence():
    import retire_lsp_evidence

    record = {"live": False, "failure_evidence": True, "failure_age_days": 100,
              "processes_dead": False}
    assert retire_lsp_evidence._dead_evidence([record]) == []
    assert not retire_lsp_evidence._retirable(record)


def test_unknown_lsp_owner_is_not_reported_as_a_proven_crash(monkeypatch):
    import time
    from datetime import datetime, timezone

    import doctor

    _observe(monkeypatch, None)
    owner, _lease = _lsp_records(_identity((HOST, BOOT, "4", "101", "4.200")))
    now = datetime(2026, 9, 29, 12, 0, 3, tzinfo=timezone.utc)
    snapshot = ("a" * 32, {"owner.json"}, owner, None, None)
    result = doctor._read_lsp_owner(snapshot, now, time.monotonic() + SHORT_TIMEOUT)
    assert result.unreadable
    assert not result.record["processes_dead"]
    assert not result.record["failure_evidence"]
