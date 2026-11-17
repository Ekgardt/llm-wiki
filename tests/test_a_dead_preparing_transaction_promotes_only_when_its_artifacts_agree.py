from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from llm_wiki import markdown_recovery
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.markdown_records import TargetStateMismatch
from markdown_transaction import (
    MarkdownChange,
    MarkdownCoordinator,
    sha256_bytes,
)

_NOTE = "knowledge/notes/page.md"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    return root


@pytest.fixture
def coordinator(vault: Path, tmp_path: Path) -> MarkdownCoordinator:
    return MarkdownCoordinator(vault, tmp_path / "state")


def _dead_preparing(coordinator: MarkdownCoordinator, *paths: str) -> str:
    changes = [MarkdownChange.create(path, b"page\n") for path in paths or (_NOTE,)]
    record = coordinator.prepare(changes, operation_id="dead-preparing")
    with coordinator._connect() as database:
        database.execute(
            "UPDATE \"transaction\" SET state='preparing', owner_pid=NULL WHERE id=?",
            (record.id,),
        )
        database.execute('DELETE FROM "operation" WHERE transaction_id=?', (record.id,))
    return record.id


def _artifact(coordinator: MarkdownCoordinator, transaction_id: str, name: str) -> Path:
    return coordinator.transaction_root / transaction_id / name


def _document(coordinator: MarkdownCoordinator, transaction_id: str, name: str) -> Any:
    return json.loads(_artifact(coordinator, transaction_id, name).read_bytes())


def _rewrite_manifest(
    coordinator: MarkdownCoordinator, transaction_id: str, edit: Callable[[Any], None]
) -> None:
    manifest = _document(coordinator, transaction_id, "manifest.json")
    edit(manifest)
    _artifact(coordinator, transaction_id, "manifest.json").write_bytes(
        canonical_json_bytes(manifest)
    )


def _rewrite_plan(
    coordinator: MarkdownCoordinator, transaction_id: str, edit: Callable[[Any], None]
) -> None:
    plan = _document(coordinator, transaction_id, "plan.json")
    edit(plan)
    plan_bytes = canonical_json_bytes(plan)
    _artifact(coordinator, transaction_id, "plan.json").write_bytes(plan_bytes)

    def rehash(manifest: Any) -> None:
        manifest["plan_hash"] = sha256_bytes(plan_bytes)

    _rewrite_manifest(coordinator, transaction_id, rehash)


def _setter(field: str, value: object) -> Callable[[Any], None]:
    def assign(document: Any) -> None:
        document[field] = value

    return assign


def _operation_setter(field: str, value: object) -> Callable[[Any], None]:
    def assign(document: Any) -> None:
        document["operations"][0][field] = value

    return assign


def _verdict(coordinator: MarkdownCoordinator, transaction_id: str) -> tuple[str, str]:
    record = coordinator._record(transaction_id)
    return (coordinator._promote_preparing(record), coordinator._record(transaction_id).state)


def test_an_untouched_dead_preparing_transaction_is_promoted_and_committed(
    coordinator: MarkdownCoordinator, vault: Path
) -> None:
    transaction_id = _dead_preparing(coordinator)

    recovered = coordinator.recover()

    assert [(record.id, record.state) for record in recovered] == [(transaction_id, "committed")]
    assert (vault / _NOTE).read_bytes() == b"page\n"


@pytest.mark.parametrize(
    "edit",
    [
        _setter("schema_version", "markdown-transaction-recovery/v0"),
        _setter("transaction_id", "f" * 32),
        _setter("operations", "none"),
        _setter("operations", []),
        _setter("request_hash", "0" * 64),
        _operation_setter("position", False),
        _operation_setter("parent_device", "1"),
        _operation_setter("parent_device", 1 << 64),
        _operation_setter("before_hash", "0" * 64),
        _operation_setter("after_hash", "0" * 64),
    ],
)
def test_a_manifest_that_disagrees_with_its_plan_is_discarded(
    coordinator: MarkdownCoordinator, vault: Path, edit: Callable[[Any], None]
) -> None:
    transaction_id = _dead_preparing(coordinator)
    _rewrite_manifest(coordinator, transaction_id, edit)

    assert _verdict(coordinator, transaction_id) == ("invalid", "preparing")
    recovered = coordinator.recover()
    assert [(record.id, record.state) for record in recovered] == [(transaction_id, "discarded")]
    assert not (vault / _NOTE).exists()
    assert not _artifact(coordinator, transaction_id, "plan.json").exists()


def test_a_manifest_that_is_not_canonical_is_discarded(
    coordinator: MarkdownCoordinator,
) -> None:
    transaction_id = _dead_preparing(coordinator)
    manifest_path = _artifact(coordinator, transaction_id, "manifest.json")
    manifest_path.write_text(json.dumps(json.loads(manifest_path.read_bytes()), indent=2))

    assert _verdict(coordinator, transaction_id) == ("invalid", "preparing")


def test_a_plan_that_is_not_canonical_is_discarded(
    coordinator: MarkdownCoordinator,
) -> None:
    transaction_id = _dead_preparing(coordinator)
    plan_path = _artifact(coordinator, transaction_id, "plan.json")
    plan_path.write_text(json.dumps(json.loads(plan_path.read_bytes()), indent=2))

    assert _verdict(coordinator, transaction_id) == ("invalid", "preparing")


def test_a_plan_written_for_another_transaction_is_discarded(
    coordinator: MarkdownCoordinator,
) -> None:
    transaction_id = _dead_preparing(coordinator)
    _rewrite_plan(coordinator, transaction_id, _setter("transaction_id", "f" * 32))

    assert _verdict(coordinator, transaction_id) == ("invalid", "preparing")


def test_two_plan_paths_that_differ_only_by_case_are_discarded(
    coordinator: MarkdownCoordinator,
) -> None:
    transaction_id = _dead_preparing(coordinator, "knowledge/notes/a.md", "knowledge/notes/b.md")

    def collide(plan: Any) -> None:
        plan["operations"][1]["path"] = "knowledge/notes/A.md"

    _rewrite_plan(coordinator, transaction_id, collide)

    assert _verdict(coordinator, transaction_id) == ("invalid", "preparing")


def test_a_plan_path_outside_every_root_is_quarantined_as_a_boundary_change(
    coordinator: MarkdownCoordinator, vault: Path
) -> None:
    transaction_id = _dead_preparing(coordinator)
    _rewrite_plan(coordinator, transaction_id, _operation_setter("path", "knowledge/index.md/x.md"))

    recovered = coordinator.recover()

    assert [(record.state, record.error_code) for record in recovered] == [
        ("quarantined", "parent_identity_changed")
    ]
    assert not (vault / _NOTE).exists()


def test_a_parent_that_cannot_be_identified_quarantines_the_promotion(
    coordinator: MarkdownCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction_id = _dead_preparing(coordinator)

    def unidentifiable(parent: Path) -> tuple[int, int]:
        raise RuntimeError(f"cannot identify {parent}")

    monkeypatch.setattr(coordinator, "_parent_identity", unidentifiable)

    assert _verdict(coordinator, transaction_id) == ("quarantined", "quarantined")
    assert coordinator._record(transaction_id).error_code == "parent_identity_changed"


def test_a_promotion_raced_by_another_recoverer_is_not_written_twice(
    coordinator: MarkdownCoordinator,
) -> None:
    transaction_id = _dead_preparing(coordinator)
    stale = coordinator._record(transaction_id)
    with coordinator._connect() as database:
        database.execute(
            "UPDATE \"transaction\" SET state='prepared' WHERE id=?", (transaction_id,)
        )

    assert coordinator._promote_preparing(stale) == "invalid"
    assert coordinator._operation_rows(transaction_id) == []


def test_a_manifest_for_an_unknown_transaction_does_not_match_any_request(
    coordinator: MarkdownCoordinator,
) -> None:
    transaction_id = _dead_preparing(coordinator)
    record = coordinator._record(transaction_id)
    manifest = _document(coordinator, transaction_id, "manifest.json")

    unknown = dataclasses.replace(record, id="0" * 32)

    assert coordinator._manifest_matches_request(unknown, manifest) is False


@pytest.mark.parametrize("bound", [-1, True, "1"])
def test_a_recovery_bound_that_is_not_a_count_is_refused(
    coordinator: MarkdownCoordinator, bound: object
) -> None:
    with pytest.raises(ValueError, match="max_transactions must be a non-negative integer"):
        coordinator.recover(max_transactions=bound)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ({"sha256": 5, "length": 3}, "invalid transaction state hash"),
        ({"sha256": "a" * 64}, "invalid transaction state description"),
        ("present", "invalid transaction state description"),
    ],
)
def test_a_malformed_state_description_is_refused(
    coordinator: MarkdownCoordinator, state: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        coordinator._state_description_hash(state)


def test_an_unknown_operation_kind_places_no_absence_rule() -> None:
    assert markdown_recovery._kind_transition_valid("rename", "a" * 64, "b" * 64) is True


def test_a_persisted_operation_beside_a_plan_entry_that_is_no_mapping_is_refused() -> None:
    persisted = {
        "position": 0,
        "before_hash": "a" * 64,
        "after_hash": "b" * 64,
        "parent_device": 1,
        "parent_inode": 2,
    }

    assert markdown_recovery._validated_persisted_operation(persisted, 0, "entry") is None


def test_a_failure_that_is_no_state_mismatch_is_raised_unchanged(
    coordinator: MarkdownCoordinator,
) -> None:
    transaction_id = _dead_preparing(coordinator)
    failure = ValueError("not a mismatch")

    with pytest.raises(ValueError) as raised:
        coordinator._conflicted_from_mismatch(transaction_id, failure)

    assert raised.value is failure


def test_an_after_state_mismatch_conflicts_as_unknown_target_bytes(
    coordinator: MarkdownCoordinator,
) -> None:
    transaction_id = _dead_preparing(coordinator)

    record = coordinator._conflicted_from_mismatch(
        transaction_id, TargetStateMismatch("after", _NOTE)
    )

    assert (record.state, record.error_code) == ("conflicted", "unknown_target_bytes")


def test_an_unnamed_state_mismatch_is_not_settled_here(
    coordinator: MarkdownCoordinator,
) -> None:
    transaction_id = _dead_preparing(coordinator)
    mismatch = TargetStateMismatch("during", _NOTE)

    with pytest.raises(TargetStateMismatch):
        coordinator._conflicted_from_mismatch(transaction_id, mismatch)
