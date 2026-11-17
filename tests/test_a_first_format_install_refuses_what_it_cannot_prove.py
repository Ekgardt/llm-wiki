from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from install_state import (
    MAX_PREIMAGE_BYTES,
    InstallControlError,
    ManagedResource,
    file_resource,
    sha256,
)
from llm_wiki import install_transactions as transactions
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.install_records import prepare_install_root
from llm_wiki.install_v2_transactions import install_resources

from tests.install_control_helpers import _release

_Record = dict[str, object]


class _Scripted:
    def __init__(self, reads: list[bytes | None]) -> None:
        self.reads = reads
        self.writes: list[bytes | None] = []

    def read(self) -> bytes | None:
        if len(self.reads) > 1:
            return self.reads.pop(0)
        return self.reads[0]

    def write(self, value: bytes | None) -> None:
        self.writes.append(value)


def _resource(
    scripted: _Scripted,
    *,
    desired: bytes = b"wanted",
    resource_id: str = "thing",
    kind: str = "config_file",
    adopt_as_absent: bool = False,
    definitions: dict[str, bytes] | None = None,
) -> ManagedResource:
    return ManagedResource(
        resource_id=resource_id,
        kind=kind,
        locator=f"/managed/{resource_id}",
        desired=desired,
        read_owned=scripted.read,
        write_owned=scripted.write,
        recognizes=lambda _value: True,
        adopt_as_absent=adopt_as_absent,
        definitions=definitions or {},
    )


def _install(tmp_path: Path, resources: list[ManagedResource]) -> _Record:
    return install_resources(
        state_root=tmp_path / "state",
        vault_root=tmp_path,
        release=_release(),
        scheduler_backend="cron",
        resources=resources,
        control_version=1,
    )


def _config(tmp_path: Path, desired: bytes = b"hello") -> ManagedResource:
    return file_resource(
        resource_id="cfg", kind="config_file", path=tmp_path / "config", desired=desired
    )


def _install_root(tmp_path: Path) -> Path:
    return tmp_path / "state" / "run" / "install"


def _transaction(tmp_path: Path) -> _Record:
    value: _Record = json.loads((_install_root(tmp_path) / "transaction.json").read_bytes())
    return value


def _edit_transaction(tmp_path: Path, **changes: object) -> None:
    path = _install_root(tmp_path) / "transaction.json"
    path.write_bytes(canonical_json_bytes({**_transaction(tmp_path), **changes}))


def _refused(code: str, action: Callable[[], object]) -> None:
    """The refusal's code is the message up to its first colon, not text that contains it."""
    with pytest.raises(InstallControlError) as raised:
        action()
    assert str(raised.value).split(":", 1)[0] == code


def test_a_request_without_resources_is_refused() -> None:
    _refused("install_resource_count_invalid", lambda: transactions.validate_resources([]))


def test_a_request_naming_one_resource_twice_is_refused() -> None:
    resource = _resource(_Scripted([None]))
    _refused(
        "install_resource_id_duplicate",
        lambda: transactions.validate_resources([resource, resource]),
    )


def test_two_resources_sharing_a_name_are_refused() -> None:
    first = _resource(_Scripted([None]), kind="config_file")
    second = _resource(_Scripted([None]), kind="profile_fragment")
    _refused(
        "install_resource_id_duplicate",
        lambda: transactions.validate_unique_names([first, second]),
    )


def test_a_resource_larger_than_a_preimage_is_refused() -> None:
    resource = _resource(_Scripted([None]), desired=b"x" * (MAX_PREIMAGE_BYTES + 1))
    _refused("install_resource_too_large", lambda: transactions.validate_resources([resource]))


def test_an_unknown_scheduler_backend_is_refused() -> None:
    _refused("install_scheduler_backend_invalid", lambda: transactions.validate_backend("at"))


def test_a_stored_preimage_with_other_bytes_is_a_conflict(tmp_path: Path) -> None:
    install_root = prepare_install_root(tmp_path)
    (install_root / "preimages" / f"{sha256(b'origin')}.bin").write_bytes(b"tampered")
    _refused(
        "install_preimage_conflict", lambda: transactions.write_preimage(install_root, b"origin")
    )


def test_a_preimage_that_cannot_be_synced_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_root = prepare_install_root(tmp_path)

    def fail(_descriptor: int) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="disk gone"):
        transactions.write_preimage(install_root, b"origin")
    assert list((install_root / "preimages").iterdir()) == []


@pytest.mark.parametrize(
    ("relative", "size", "code"),
    [
        ("/scheduler/abs", 1, "install_definition_path_invalid"),
        ("scheduler/../escape", 1, "install_definition_path_invalid"),
        ("elsewhere/unit", 1, "install_definition_path_invalid"),
        ("scheduler/huge", MAX_PREIMAGE_BYTES + 1, "install_definition_too_large"),
    ],
    ids=["absolute", "parent", "outside-scheduler", "oversized"],
)
def test_a_definition_the_install_root_cannot_hold_is_refused(
    tmp_path: Path, relative: str, size: int, code: str
) -> None:
    resource = _resource(_Scripted([None]), definitions={relative: b"x" * size})
    _refused(code, lambda: transactions.persist_resource_definition(tmp_path, resource))


def test_a_resource_adopted_as_absent_keeps_no_origin() -> None:
    resource = _resource(_Scripted([None]), adopt_as_absent=True)
    assert transactions.resource_origin(resource, b"present") is None


def test_a_write_that_does_not_stick_is_rolled_back(tmp_path: Path) -> None:
    scripted = _Scripted([None])
    _refused(
        "install_resource_verification_failed",
        lambda: _install(tmp_path, [_resource(scripted)]),
    )
    assert scripted.writes == [b"wanted"]
    assert _transaction(tmp_path)["state"] == "reverted"


def test_a_resource_changed_between_prepare_and_write_is_drift(tmp_path: Path) -> None:
    scripted = _Scripted([None, b"someone else"])
    _refused("install_rollback_quarantined", lambda: _install(tmp_path, [_resource(scripted)]))
    assert _transaction(tmp_path)["error"] == {"code": "install_rollback_drift"}
    assert _transaction(tmp_path)["state"] == "quarantined"


def test_a_restore_that_does_not_stick_is_quarantined(tmp_path: Path) -> None:
    first = _Scripted([None, None, b"wanted"])
    second = _Scripted([None])
    resources = [_resource(first), _resource(second, resource_id="other")]
    _refused("install_rollback_quarantined", lambda: _install(tmp_path, resources))
    assert _transaction(tmp_path)["error"] == {"code": "install_rollback_verification_failed"}
    assert first.writes == [b"wanted", None]


def test_a_transaction_whose_resources_are_not_a_list_is_invalid() -> None:
    _refused(
        "install_transaction_invalid",
        lambda: transactions._manifest_from_transaction({"resources": {}}),
    )


def test_a_published_manifest_with_other_bytes_is_a_conflict(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_bytes(b"other")
    _refused(
        "install_manifest_conflict",
        lambda: transactions._publish_manifest_bytes(path, b"mine"),
    )


def test_an_already_published_manifest_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_bytes(b"mine")
    transactions._publish_manifest_bytes(path, b"mine")
    assert path.read_bytes() == b"mine"


def test_a_record_without_an_origin_snapshot_is_invalid() -> None:
    _refused("install_transaction_invalid", lambda: transactions._origin_size({"origin": None}))


def test_preimages_beyond_their_total_bound_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transactions, "MAX_TOTAL_PREIMAGE_BYTES", 4)
    resource = _resource(_Scripted([b"existing origin"]))
    _refused("install_preimages_too_large", lambda: _install(tmp_path, [resource]))


def test_a_second_install_of_another_release_is_not_an_upgrade(tmp_path: Path) -> None:
    _install(tmp_path, [_config(tmp_path)])
    _refused(
        "install_upgrade_not_supported",
        lambda: _install(tmp_path, [_config(tmp_path, b"changed")]),
    )


def test_a_second_install_over_a_changed_file_is_drift(tmp_path: Path) -> None:
    _install(tmp_path, [_config(tmp_path)])
    (tmp_path / "config").write_bytes(b"edited")
    _refused("install_resource_drift", lambda: _install(tmp_path, [_config(tmp_path)]))


def test_a_manifest_the_transaction_did_not_name_is_refused(tmp_path: Path) -> None:
    _install(tmp_path, [_config(tmp_path)])
    _edit_transaction(tmp_path, target_manifest_sha256="0" * 64)
    _refused(
        "install_manifest_transaction_mismatch", lambda: _install(tmp_path, [_config(tmp_path)])
    )


def test_a_manifest_published_before_the_commit_completes_it(tmp_path: Path) -> None:
    manifest = _install(tmp_path, [_config(tmp_path)])
    _edit_transaction(tmp_path, state="publishing")
    assert _install(tmp_path, [_config(tmp_path)]) == manifest
    assert _transaction(tmp_path)["state"] == "committed"


def test_a_manifest_beside_an_unfinished_transaction_is_a_conflict(tmp_path: Path) -> None:
    _install(tmp_path, [_config(tmp_path)])
    _edit_transaction(tmp_path, state="mutating")
    _refused(
        "install_transaction_manifest_conflict", lambda: _install(tmp_path, [_config(tmp_path)])
    )


def test_a_manifest_without_its_transaction_is_refused(tmp_path: Path) -> None:
    _install(tmp_path, [_config(tmp_path)])
    (_install_root(tmp_path) / "transaction.json").unlink()
    _refused(
        "install_manifest_without_transaction", lambda: _install(tmp_path, [_config(tmp_path)])
    )


def _interrupted(tmp_path: Path, state: str) -> _Record:
    manifest = _install(tmp_path, [_config(tmp_path)])
    (_install_root(tmp_path) / "manifest.json").unlink()
    _edit_transaction(tmp_path, state=state)
    return manifest


def test_an_install_interrupted_while_publishing_publishes_on_resume(tmp_path: Path) -> None:
    manifest = _interrupted(tmp_path, "publishing")
    assert _install(tmp_path, [_config(tmp_path)]) == manifest
    assert _transaction(tmp_path)["state"] == "committed"


def test_a_quarantined_transaction_blocks_a_new_install(tmp_path: Path) -> None:
    _interrupted(tmp_path, "quarantined")
    _refused("install_transaction_blocks_new_work", lambda: _install(tmp_path, [_config(tmp_path)]))


def test_a_resumed_install_whose_record_names_other_resources_is_refused(
    tmp_path: Path,
) -> None:
    _interrupted(tmp_path, "mutating")
    records = _transaction(tmp_path)["resources"]
    assert isinstance(records, list)
    _edit_transaction(tmp_path, resources=[{**records[0], "locator": str(tmp_path / "moved")}])
    _refused("install_resource_request_mismatch", lambda: _install(tmp_path, [_config(tmp_path)]))
