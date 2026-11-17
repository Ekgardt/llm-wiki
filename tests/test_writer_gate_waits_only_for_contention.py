from __future__ import annotations

import dataclasses
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, cast

import lease_renewal
import pytest
from llm_wiki import markdown_writer_gate
from markdown_transaction import MarkdownCoordinator

from tests.transaction_abort_helpers import _abort_fixture


def _busy() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


_LOCKED = _busy()


class _Canonical(NamedTuple):
    coordinator: MarkdownCoordinator
    registry: Any
    owner: Any


@pytest.fixture
def canonical(tmp_path: Path) -> _Canonical:
    coordinator, registry, owner, _fence, _binding, _record = _abort_fixture(tmp_path)
    return _Canonical(coordinator, registry, owner)


@pytest.fixture
def plain(tmp_path: Path) -> MarkdownCoordinator:
    vault = tmp_path / "vault"
    (vault / "knowledge/notes").mkdir(parents=True)
    return MarkdownCoordinator(vault, tmp_path / "state")


def _recorded_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    clock = SimpleNamespace(sleep=sleeps.append, monotonic=time.monotonic)
    monkeypatch.setattr(markdown_writer_gate, "time", clock)
    return sleeps


@pytest.mark.parametrize("wait", [-1, True, "1"])
def test_a_wait_that_is_not_a_non_negative_number_is_refused(wait: object) -> None:
    with pytest.raises(ValueError, match="wait_seconds must be non-negative or None"):
        markdown_writer_gate._require_gate_wait(wait)
    with pytest.raises(ValueError, match="wait_seconds must be non-negative or None"):
        markdown_writer_gate._require_wait_seconds(wait)


def test_a_failure_that_is_no_contention_ends_the_gate_wait_at_once() -> None:
    failure = sqlite3.DatabaseError("database disk image is malformed")

    with pytest.raises(sqlite3.DatabaseError) as raised:
        markdown_writer_gate.retry_gate_or_fail(failure, 0, time.monotonic() + 60)

    assert raised.value is failure


def test_contention_inside_the_window_sleeps_and_counts_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps = _recorded_sleeps(monkeypatch)

    attempt = markdown_writer_gate.retry_gate_or_fail(_LOCKED, 2, time.monotonic() + 60)

    assert attempt == 3
    assert len(sleeps) == 1 and sleeps[0] > 0


@pytest.mark.parametrize(
    ("failure", "deadline_offset"),
    [(sqlite3.DatabaseError("malformed"), 60.0), (_LOCKED, -1.0)],
)
def test_a_heartbeat_that_cannot_be_retried_loses_the_gate(
    failure: BaseException, deadline_offset: float
) -> None:
    deadline = time.monotonic() + deadline_offset

    assert markdown_writer_gate._retried_heartbeat(failure, 0, deadline, threading.Event()) is None


def test_a_canonical_lease_refusal_that_is_not_busy_is_raised() -> None:
    refusal = RuntimeError("owner_lost")

    with pytest.raises(RuntimeError) as raised:
        markdown_writer_gate._canonical_lease_retry_delay(refusal, 0, time.monotonic() + 60)

    assert raised.value is refusal


def test_a_legacy_gate_claim_that_met_contention_is_retried(
    plain: MarkdownCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps = _recorded_sleeps(monkeypatch)
    answers: list[object] = [_LOCKED, 5]

    def claim(owner_token: str, deadline: float) -> int:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, int)
        return answer

    monkeypatch.setattr(plain, "_try_claim_legacy_gate", claim)

    assert plain._acquire_legacy_gate("token", time.monotonic() + 60) == 5
    assert (len(sleeps), answers) == (1, [])


def test_a_nested_gate_owner_must_be_an_owner_lease(plain: MarkdownCoordinator) -> None:
    with (
        pytest.raises(TypeError, match="owner must be an OwnerLease"),
        plain.writer_gate(owner=cast(Any, "owner")),
    ):
        pass


def test_a_nested_gate_needs_a_canonical_coordinator(
    plain: MarkdownCoordinator, canonical: _Canonical
) -> None:
    with (
        pytest.raises(RuntimeError, match="requires a v3 coordinator"),
        plain.writer_gate(owner=canonical.owner),
    ):
        pass


def test_a_nested_gate_reentered_by_another_owner_is_refused(canonical: _Canonical) -> None:
    stranger = dataclasses.replace(canonical.owner, token="another-token")
    coordinator = canonical.coordinator

    with (
        coordinator.writer_gate(owner=canonical.owner),
        pytest.raises(RuntimeError, match="nested writer owner changed"),
        coordinator.writer_gate(owner=stranger),
    ):
        pass

    assert coordinator.writer_gate_held() is False


def test_a_projection_that_is_gone_cannot_be_renewed_or_deleted(
    canonical: _Canonical,
) -> None:
    coordinator = canonical.coordinator

    with pytest.raises(RuntimeError, match="writer projection was lost"):
        coordinator._renew_canonical_writer_gate(canonical.registry, canonical.owner)
    with (
        coordinator._connect() as database,
        pytest.raises(RuntimeError, match="another owner holds the gate"),
    ):
        coordinator._delete_writer_projection(database, canonical.owner)


def test_a_held_projection_is_renewed_with_the_canonical_lease(
    canonical: _Canonical,
) -> None:
    coordinator = canonical.coordinator

    with coordinator.writer_gate(owner=canonical.owner):
        coordinator._renew_canonical_writer_gate(canonical.registry, canonical.owner)
        with coordinator._connect() as database:
            rows = database.execute(
                "SELECT owner_token FROM writer_owners WHERE gate_name='global'"
            ).fetchall()

    assert [row[0] for row in rows] == [canonical.owner.token]


@pytest.mark.parametrize(
    ("failure", "settled"),
    [(_LOCKED, False), (sqlite3.DatabaseError("malformed"), True)],
)
def test_a_release_retry_settles_unless_the_database_is_only_busy(
    canonical: _Canonical,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    settled: bool,
) -> None:
    def refuse(registry: object, lease: object) -> None:
        raise failure

    monkeypatch.setattr(canonical.coordinator, "_release_canonical_lease", refuse)

    assert canonical.coordinator._release_settled(canonical.registry, canonical.owner) is settled


def test_leaving_a_gate_whose_release_failed_hard_raises_and_clears_the_gate(
    canonical: _Canonical, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = OSError(5, "input/output error")

    def refuse(registry: object, lease: object) -> None:
        raise failure

    monkeypatch.setattr(canonical.coordinator, "_release_canonical_lease", refuse)
    finished = threading.Thread(target=lambda: None)
    finished.start()
    canonical.coordinator._local.gate_depth = 1

    with pytest.raises(OSError) as raised:
        canonical.coordinator._leave_canonical_gate(
            canonical.registry, canonical.owner, threading.Event(), finished
        )

    assert raised.value is failure
    assert canonical.coordinator.writer_gate_held() is False


def test_a_heartbeat_whose_renewal_ended_marks_the_gate_lost(
    canonical: _Canonical, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ended(*arguments: object, **options: object) -> str:
        return "owner_lost"

    monkeypatch.setattr(lease_renewal, "renew_until_stopped", ended)
    lost = threading.Event()

    canonical.coordinator._heartbeat_canonical_writer_gate(
        canonical.registry, canonical.owner, threading.Event(), lost, time.monotonic()
    )

    assert lost.is_set()


def test_a_gate_row_that_no_longer_names_the_owner_is_lost_before_mutation(
    canonical: _Canonical,
) -> None:
    coordinator = canonical.coordinator

    with (
        coordinator._connect() as database,
        pytest.raises(RuntimeError, match="ownership was lost before mutation"),
    ):
        coordinator._refresh_v3_writer_lease(
            database, canonical.owner, "another-token", canonical.owner.epoch
        )
