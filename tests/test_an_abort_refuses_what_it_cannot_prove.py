from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple, cast

import markdown_transaction
import pytest
from llm_wiki import markdown_abort
from markdown_transaction import MarkdownCoordinator, TransactionFailure

from tests.transaction_abort_helpers import _abort_fixture

_ACTOR = "posix-uid:1000"


class _Abort(NamedTuple):
    coordinator: MarkdownCoordinator
    fence: Any
    digest: str
    transaction_id: str
    notes: Path


@pytest.fixture
def abort(tmp_path: Path) -> _Abort:
    coordinator, _registry, _owner, fence, binding, record = _abort_fixture(tmp_path)
    return _Abort(
        coordinator, fence, binding.active_digest, record.id, tmp_path / "knowledge/notes"
    )


def _discard(abort: _Abort, **overrides: object) -> object:
    arguments: dict[str, object] = {
        "intent_fence": abort.fence,
        "active_link_digest": abort.digest,
        "actor_identity": _ACTOR,
    }
    arguments.update(overrides)
    return abort.coordinator.abort_for_discard(
        abort.transaction_id, **cast(dict[str, Any], arguments)
    )


def _state(abort: _Abort) -> tuple[str, str | None]:
    with sqlite3.connect(abort.coordinator.database_path) as database:
        row = database.execute(
            'SELECT state, error_code FROM "transaction" WHERE id=?',
            (abort.transaction_id,),
        ).fetchone()
    return (row[0], row[1])


def _set_state(abort: _Abort, state: str) -> None:
    with abort.coordinator._connect() as database:
        database.execute(
            'UPDATE "transaction" SET state=? WHERE id=?', (state, abort.transaction_id)
        )


def _failure_code(abort: _Abort) -> str:
    with pytest.raises(TransactionFailure) as raised:
        _discard(abort)
    return raised.value.code


def _on_killpoint(abort: _Abort, name: str, action: Callable[[], object]) -> None:
    def killpoint(point: str, _parent: str | None = None) -> None:
        if point == name:
            action()

    vars(abort.coordinator)["_killpoint"] = killpoint


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"intent_fence": "operator"}, "abort requires an operator intent fence"),
        ({"active_link_digest": "A" * 64}, "active link digest must be lowercase 64-hex"),
        ({"actor_identity": 1000}, "actor identity is invalid"),
        ({"actor_identity": ""}, "actor identity is invalid"),
        ({"actor_identity": "a" * 513}, "actor identity is invalid"),
    ],
)
def test_an_abort_without_a_valid_operator_and_actor_is_refused(
    abort: _Abort, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _discard(abort, **overrides)

    assert _state(abort) == ("applying", None)


def test_an_abort_under_a_capture_fence_is_refused(abort: _Abort) -> None:
    capture_fence = dataclasses.replace(abort.fence, mode="capture")

    with pytest.raises(ValueError, match="abort requires an operator intent fence"):
        _discard(abort, intent_fence=capture_fence)


def test_an_abort_of_a_transaction_without_operations_is_unknown(abort: _Abort) -> None:
    with pytest.raises(KeyError):
        abort.coordinator.abort_for_discard(
            "0" * 32,
            intent_fence=abort.fence,
            active_link_digest=abort.digest,
            actor_identity=_ACTOR,
        )


@pytest.mark.parametrize("state", ["conflicted", "quarantined", "discarded"])
def test_a_settled_transaction_is_not_aborted(abort: _Abort, state: str) -> None:
    _set_state(abort, state)

    assert (_failure_code(abort), _state(abort)) == ("abort_state_refused", (state, None))
    assert (abort.notes / "replace.md").read_bytes() == b"replace-after"


def _crash(message: str) -> Callable[[], None]:
    def crash() -> None:
        raise RuntimeError(message)

    return crash


def test_a_second_abort_adopts_the_direction_the_first_chose(abort: _Abort) -> None:
    _on_killpoint(abort, "after_aborting", _crash("crash after direction"))
    with pytest.raises(RuntimeError, match="crash after direction"):
        _discard(abort)
    with abort.coordinator._connect() as database:
        chosen = database.execute(
            'SELECT abort_operation_id FROM "transaction" WHERE id=?',
            (abort.transaction_id,),
        ).fetchone()[0]
    _on_killpoint(abort, "never", _crash("unused"))

    receipt = cast(markdown_transaction.TransactionAbortReceipt, _discard(abort))

    assert (receipt.abort_operation_id, _state(abort)) == (chosen, ("aborted", None))


def test_a_target_already_back_at_its_before_bytes_is_left_alone(abort: _Abort) -> None:
    (abort.notes / "replace.md").write_bytes(b"replace-before")

    _discard(abort)

    assert (abort.notes / "replace.md").read_bytes() == b"replace-before"
    assert _state(abort) == ("aborted", None)


def test_a_receipt_that_reads_back_differently_is_a_conflict(
    abort: _Abort, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(markdown_abort, "read_runtime_bytes", lambda *arguments, **options: b"{}")

    assert _failure_code(abort) == "abort_receipt_conflict"
    assert _state(abort)[0] == "aborting"


def test_a_target_written_after_the_restore_fails_final_verification(
    abort: _Abort,
) -> None:
    replace = abort.notes / "replace.md"
    _on_killpoint(abort, "after_abort_receipt", lambda: replace.write_bytes(b"late writer"))

    assert _failure_code(abort) == "abort_final_verification_failed"
    assert (_state(abort)[0], replace.read_bytes()) == ("aborting", b"late writer")


def test_an_abort_whose_direction_changed_underneath_loses_its_fence(abort: _Abort) -> None:
    def redirect() -> None:
        with abort.coordinator._connect() as database:
            database.execute(
                'UPDATE "transaction" SET abort_operation_id=? WHERE id=?',
                ("transaction-abort:other", abort.transaction_id),
            )

    _on_killpoint(abort, "after_abort_receipt", redirect)

    assert _failure_code(abort) == "abort_fence_lost"
    assert _state(abort)[0] == "aborting"


def _crashed_after_direction(abort: _Abort) -> None:
    _on_killpoint(abort, "after_aborting", _crash("crash after direction"))
    with pytest.raises(RuntimeError, match="crash after direction"):
        _discard(abort)


def _recovered_state(abort: _Abort) -> tuple[str, str | None]:
    recovered = MarkdownCoordinator.from_v3_candidate(
        abort.coordinator.database_path, state_root=abort.notes.parents[1]
    ).recover()
    return (recovered[0].state, recovered[0].error_code)


def test_recovery_stops_an_abort_at_third_party_bytes(abort: _Abort) -> None:
    _crashed_after_direction(abort)
    (abort.notes / "delete.md").write_bytes(b"third-party")

    assert _recovered_state(abort) == ("aborting", "abort_target_conflict")
    assert (abort.notes / "delete.md").read_bytes() == b"third-party"


def test_recovery_stops_an_abort_at_a_corrupt_before_image(abort: _Abort) -> None:
    _crashed_after_direction(abort)
    rows = abort.coordinator._operation_rows(abort.transaction_id)
    last = rows[-1]
    image = (
        abort.notes.parents[1]
        / f"run/transactions/{abort.transaction_id}/before/{int(last['position']):06d}.bin"
    )
    image.write_bytes(b"corrupted-before-image")

    assert _recovered_state(abort) == ("aborting", "abort_before_image_corrupt")
    assert not (abort.notes / "delete.md").exists()


def test_recovery_of_an_abort_restores_what_is_left_and_awaits_its_receipt(
    abort: _Abort,
) -> None:
    _crashed_after_direction(abort)
    (abort.notes / "replace.md").write_bytes(b"replace-before")

    assert _recovered_state(abort) == ("aborting", "abort_receipt_pending")
    assert not (abort.notes / "create.md").exists()
    assert (abort.notes / "replace.md").read_bytes() == b"replace-before"
