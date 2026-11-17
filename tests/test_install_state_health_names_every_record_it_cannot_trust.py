from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from pathlib import Path

import install_state
import pytest
from install_state import (
    MAX_PREIMAGE_BYTES,
    MAX_RECORD_BYTES,
    InstallControlError,
    file_resource,
    manifest_digest,
    validate_install_state,
)
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.install_uninstall_rollback import uninstall_resources
from llm_wiki.install_v2_transactions import install_resources

from tests.install_control_helpers import _release

_Record = dict[str, object]

_ABSENT = {"codes": [], "deletion_codes": [], "health": "ok", "status": "absent"}
_ACTIVE = {
    "codes": [],
    "deletion_codes": ["install_manifest_retained"],
    "health": "ok",
    "status": "active",
}
_CORRUPT = {
    "codes": ["install_state_corrupt"],
    "deletion_codes": ["install_state_corrupt"],
    "health": "error",
    "status": "corrupt",
}
_NONTERMINAL_ACTIVE = {
    "codes": ["install_transaction_nonterminal"],
    "deletion_codes": ["install_manifest_retained", "install_transaction_nonterminal"],
    "health": "degraded",
    "status": "nonterminal",
}
_QUARANTINED_ACTIVE = {
    "codes": ["install_transaction_quarantined"],
    "deletion_codes": ["install_manifest_retained", "install_transaction_quarantined"],
    "health": "error",
    "status": "quarantined",
}


def _install(tmp_path: Path, version: int) -> Path:
    state_root = tmp_path / "state"
    state_root.mkdir()
    install_resources(
        state_root=state_root,
        vault_root=tmp_path,
        release=_release(),
        scheduler_backend="cron",
        resources=[_config(tmp_path)],
        control_version=version,
    )
    return state_root


def _config(tmp_path: Path) -> install_state.ManagedResource:
    return file_resource(
        resource_id="cfg", kind="config_file", path=tmp_path / "config", desired=b"hello"
    )


def _install_root(state_root: Path) -> Path:
    return state_root / "run" / "install"


def _read(state_root: Path, name: str) -> _Record:
    value: _Record = json.loads((_install_root(state_root) / name).read_bytes())
    return value


def _write(state_root: Path, name: str, value: _Record) -> None:
    (_install_root(state_root) / name).write_bytes(canonical_json_bytes(value))


def _edit(state_root: Path, name: str, **changes: object) -> None:
    _write(state_root, name, {**_read(state_root, name), **changes})


@pytest.fixture
def v1(tmp_path: Path) -> Path:
    return _install(tmp_path, 1)


@pytest.fixture
def v2(tmp_path: Path) -> Path:
    return _install(tmp_path, 2)


@pytest.fixture
def uninspectable_state(tmp_path: Path) -> Iterator[Path]:
    run = tmp_path / "state" / "run"
    (run / "install").mkdir(parents=True)
    run.chmod(0o000)
    yield tmp_path / "state"
    run.chmod(0o700)


def test_no_install_root_is_an_absent_install(tmp_path: Path) -> None:
    assert validate_install_state(tmp_path) == _ABSENT


def test_an_install_root_that_is_a_link_is_corrupt(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "install").symlink_to(tmp_path / "elsewhere")
    assert validate_install_state(tmp_path) == _CORRUPT


def test_an_install_root_that_is_a_file_is_corrupt(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "install").write_bytes(b"")
    assert validate_install_state(tmp_path) == _CORRUPT


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs POSIX permissions that bind the caller; root ignores them",
)
def test_an_install_root_that_cannot_be_inspected_is_corrupt(uninspectable_state: Path) -> None:
    assert validate_install_state(uninspectable_state) == _CORRUPT


def test_an_install_root_with_only_its_scaffolding_is_absent(tmp_path: Path) -> None:
    install_root = _install_root(tmp_path)
    (install_root / "preimages").mkdir(parents=True)
    (install_root / "install.lock").write_bytes(b"0")
    assert validate_install_state(tmp_path) == _ABSENT


def test_an_install_root_holding_an_unknown_file_is_corrupt(tmp_path: Path) -> None:
    install_root = _install_root(tmp_path)
    install_root.mkdir(parents=True)
    (install_root / "stray.json").write_bytes(b"{}")
    assert validate_install_state(tmp_path) == _CORRUPT


@pytest.mark.parametrize("version", [1, 2])
def test_an_uninstalled_vault_reports_uninstalled(tmp_path: Path, version: int) -> None:
    state_root = _install(tmp_path, version)
    assert validate_install_state(state_root) == _ACTIVE
    uninstall_resources(state_root=state_root, resources=[_config(tmp_path)])
    assert validate_install_state(state_root) == {
        "codes": [],
        "deletion_codes": [],
        "health": "ok",
        "status": "uninstalled",
    }


@pytest.mark.parametrize(
    ("state", "status", "health", "codes"),
    [
        ("quarantined", "quarantined", "error", ["install_transaction_quarantined"]),
        ("reverted", "rolled_back", "ok", []),
        ("prepared", "nonterminal", "degraded", ["install_transaction_nonterminal"]),
        ("mutating", "nonterminal", "degraded", ["install_transaction_nonterminal"]),
        ("reverting", "nonterminal", "degraded", ["install_transaction_nonterminal"]),
    ],
)
def test_a_transaction_without_a_manifest_reports_its_state(
    v1: Path, state: str, status: str, health: str, codes: list[str]
) -> None:
    (_install_root(v1) / "manifest.json").unlink()
    _edit(v1, "transaction.json", state=state)
    assert validate_install_state(v1) == {
        "codes": codes,
        "deletion_codes": codes,
        "health": health,
        "status": status,
    }


def test_a_committed_install_without_its_manifest_is_corrupt(v1: Path) -> None:
    (_install_root(v1) / "manifest.json").unlink()
    assert validate_install_state(v1) == _CORRUPT


def test_a_v2_transaction_without_a_manifest_reports_its_state(v2: Path) -> None:
    (_install_root(v2) / "manifest.json").unlink()
    _edit(v2, "transaction.json", state="publishing")
    assert validate_install_state(v2)["status"] == "nonterminal"


@pytest.mark.parametrize(
    ("state", "expected"),
    [("publishing", _NONTERMINAL_ACTIVE), ("quarantined", _QUARANTINED_ACTIVE)],
)
def test_a_manifest_reports_the_state_of_its_own_transaction(
    v1: Path, state: str, expected: dict[str, object]
) -> None:
    _edit(v1, "transaction.json", state=state)
    assert validate_install_state(v1) == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "mutating"},
        {"operation": "uninstall"},
        {"target_manifest_sha256": "0" * 64},
    ],
    ids=["mid-mutation", "uninstall-transaction", "other-target"],
)
def test_a_manifest_its_transaction_does_not_bind_is_corrupt(
    v1: Path, changes: dict[str, object]
) -> None:
    _edit(v1, "transaction.json", **changes)
    assert validate_install_state(v1) == _CORRUPT


@pytest.mark.parametrize("version", [1, 2])
def test_a_manifest_without_its_transaction_is_corrupt(tmp_path: Path, version: int) -> None:
    state_root = _install(tmp_path, version)
    (_install_root(state_root) / "transaction.json").unlink()
    assert validate_install_state(state_root) == _CORRUPT


def test_a_v2_manifest_that_is_not_its_transactions_projection_is_corrupt(v2: Path) -> None:
    _edit(v2, "manifest.json", committed_at="2000-01-01T00:00:00Z")
    _edit(
        v2, "transaction.json", target_manifest_sha256=manifest_digest(_read(v2, "manifest.json"))
    )
    assert validate_install_state(v2) == _CORRUPT


def _open_update(state_root: Path, **changes: object) -> None:
    manifest = _read(state_root, "manifest.json")
    update = {
        **_read(state_root, "transaction.json"),
        "base_manifest_sha256": manifest_digest(manifest),
        "generation": 2,
        "id": "f" * 32,
        "operation": "update",
        "state": "mutating",
        "target_manifest_sha256": None,
    }
    _write(state_root, "transaction.json", {**update, **changes})


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("mutating", _NONTERMINAL_ACTIVE),
        ("quarantined", _QUARANTINED_ACTIVE),
        ("reverted", _ACTIVE),
    ],
)
def test_an_update_over_a_manifest_reports_its_own_state(
    v2: Path, state: str, expected: dict[str, object]
) -> None:
    _open_update(v2, state=state)
    assert validate_install_state(v2) == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "committed"},
        {"base_manifest_sha256": "0" * 64},
        {"generation": 3},
        {"installation_id": "e" * 32},
        {"vault_root": "/elsewhere"},
    ],
    ids=["committed", "other-base", "skipped-generation", "other-installation", "other-vault"],
)
def test_an_update_not_linked_to_the_manifest_is_corrupt(
    v2: Path, changes: dict[str, object]
) -> None:
    _open_update(v2, **changes)
    assert validate_install_state(v2) == _CORRUPT


def test_preimages_past_the_total_bound_are_corrupt(
    v2: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_state, "MAX_TOTAL_PREIMAGE_BYTES", 4)
    assert validate_install_state(v2) == _CORRUPT


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"b": 1}',
        b"[]",
        b"\xff",
        canonical_json_bytes({"schema": 1}),
        canonical_json_bytes({"schema": "install-transaction/v1"}),
        canonical_json_bytes({"schema": "install-manifest/v9"}),
        canonical_json_bytes({"schema": "install-manifest/v1"}),
    ],
    ids=[
        "duplicate-key",
        "nan",
        "noncanonical",
        "not-an-object",
        "undecodable",
        "schema-not-text",
        "other-record-type",
        "unknown-schema",
        "schema-invalid",
    ],
)
def test_a_manifest_that_is_not_a_valid_record_is_corrupt(tmp_path: Path, raw: bytes) -> None:
    install_root = _install_root(tmp_path)
    install_root.mkdir(parents=True)
    (install_root / "manifest.json").write_bytes(raw)
    assert validate_install_state(tmp_path) == _CORRUPT


def test_a_record_past_the_size_bound_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_bytes(b" " * (MAX_RECORD_BYTES + 1))
    with pytest.raises(InstallControlError, match="install_state_oversized"):
        install_state.read_state_bytes(path)


def test_an_unknown_inactive_state_is_an_orphan() -> None:
    with pytest.raises(InstallControlError, match="install_transaction_orphaned"):
        install_state._inactive_health_for_state({"state": "unheard-of"})


def test_a_managed_file_that_is_a_link_is_refused(tmp_path: Path) -> None:
    (tmp_path / "target").write_bytes(b"")
    (tmp_path / "link").symlink_to(tmp_path / "target")
    with pytest.raises(InstallControlError, match="install_resource_symlink"):
        install_state.read_managed_file(tmp_path / "link")


def test_a_managed_file_that_is_a_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InstallControlError, match="install_resource_file_invalid"):
        install_state.read_managed_file(tmp_path)
    with pytest.raises(InstallControlError, match="install_resource_file_invalid"):
        install_state.remove_managed_file(tmp_path)


def test_a_managed_file_is_never_written_through_a_linked_parent(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real")
    with pytest.raises(InstallControlError, match="install_resource_parent_symlink"):
        install_state.write_managed_file(tmp_path / "linked" / "file", b"value", 0o600)
    assert list((tmp_path / "real").iterdir()) == []


def test_a_managed_file_removed_through_its_resource_is_gone(tmp_path: Path) -> None:
    resource = _config(tmp_path)
    resource.write_owned(b"hello")
    assert resource.read_owned() == b"hello"
    resource.write_owned(None)
    assert resource.read_owned() is None


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (7, "install_definition_bundle_invalid"),
        ("not base64!", "install_definition_bundle_invalid"),
        (
            base64.b64encode(b"x" * (MAX_PREIMAGE_BYTES + 1)).decode(),
            "install_definition_bundle_oversized",
        ),
    ],
    ids=["not-text", "not-base64", "oversized"],
)
def test_a_definition_value_that_cannot_be_trusted_is_refused(value: object, code: str) -> None:
    with pytest.raises(InstallControlError, match=code):
        install_state.decode_definition_value(value)


def test_a_definition_value_decodes_to_its_bytes() -> None:
    assert install_state.decode_definition_value(base64.b64encode(b"unit").decode()) == b"unit"


@pytest.mark.parametrize(
    "record",
    [{"installed": {}, "origin": "absent"}, {"installed": [], "origin": {}}],
)
def test_v1_snapshots_that_are_not_objects_are_refused(record: _Record) -> None:
    with pytest.raises(InstallControlError, match="install_transaction_invalid"):
        install_state.resource_snapshots(record)


def test_v2_snapshots_that_are_not_objects_are_refused() -> None:
    with pytest.raises(InstallControlError, match="install_transaction_invalid"):
        install_state.v2_resource_snapshots({"desired": {}, "origin": {}, "rollback": None})


@pytest.mark.parametrize("resources", [None, [{"id": "a"}, "b"]])
def test_transaction_resources_must_be_a_list_of_objects(resources: object) -> None:
    with pytest.raises(InstallControlError, match="install_transaction_invalid"):
        install_state.transaction_resources({"resources": resources})


@pytest.mark.parametrize(
    "origin",
    [
        {"state": "present"},
        {"state": "present", "preimage": "../escape.bin"},
    ],
    ids=["no-reference", "outside-preimages"],
)
def test_a_preimage_reference_outside_preimages_is_refused(tmp_path: Path, origin: _Record) -> None:
    with pytest.raises(InstallControlError, match="install_preimage_reference_invalid"):
        install_state.read_origin(tmp_path, origin)


def _origin_for(tmp_path: Path, value: bytes) -> _Record:
    (tmp_path / "preimages").mkdir(exist_ok=True)
    (tmp_path / "preimages" / "value.bin").write_bytes(value)
    return install_state.resource_snapshot(value, "preimages/value.bin")


def test_a_preimage_that_is_a_link_is_refused(tmp_path: Path) -> None:
    origin = _origin_for(tmp_path, b"value")
    (tmp_path / "preimages" / "link.bin").symlink_to(tmp_path / "preimages" / "value.bin")
    with pytest.raises(InstallControlError, match="install_preimage_invalid"):
        install_state.read_origin(tmp_path, {**origin, "preimage": "preimages/link.bin"})


def test_a_preimage_larger_than_its_bound_is_refused(tmp_path: Path) -> None:
    origin = _origin_for(tmp_path, b"x" * (MAX_PREIMAGE_BYTES + 1))
    with pytest.raises(InstallControlError, match="install_preimage_invalid"):
        install_state.read_origin(tmp_path, origin)


def _v1_records(tmp_path: Path) -> list[_Record]:
    present: _Record = {"installed": {"state": "absent"}, "origin": _origin_for(tmp_path, b"12345")}
    absent: _Record = {"installed": {"state": "absent"}, "origin": {"state": "absent"}}
    return [present, absent]


def test_v1_preimages_are_read_and_counted_against_the_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = _v1_records(tmp_path)
    install_state._validate_preimage_references(tmp_path, records)
    monkeypatch.setattr(install_state, "MAX_TOTAL_PREIMAGE_BYTES", 4)
    with pytest.raises(InstallControlError, match="install_preimages_too_large"):
        install_state._validate_preimage_references(tmp_path, records)


def test_a_record_without_a_desired_snapshot_is_refused() -> None:
    with pytest.raises(InstallControlError, match="install_state_schema_invalid"):
        install_state.record_desired({"desired": "absent"})


def test_no_manifest_has_no_digest() -> None:
    assert manifest_digest(None) is None


def _checkpointed(checkpoint: object) -> _Record:
    return {"resources": [], "rollback_point": checkpoint}


@pytest.mark.parametrize(
    "checkpoint",
    [
        "checkpoint",
        {"resources": None},
        {"resources": ["record"]},
        {"resources": [{"installed": {"state": "absent"}, "origin": "absent"}]},
    ],
    ids=["not-an-object", "no-resources", "record-not-an-object", "origin-not-an-object"],
)
def test_a_rollback_checkpoint_that_is_not_well_formed_is_refused(
    tmp_path: Path, checkpoint: object
) -> None:
    record = _checkpointed(checkpoint)
    with pytest.raises(InstallControlError, match="install_state_schema_invalid"):
        install_state.validate_v2_record_preimages(tmp_path, record)


def test_a_rollback_checkpoint_counts_its_preimages_against_the_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _origin_for(tmp_path, b"12345")
    checkpoint = {"resources": [{"installed": {"state": "absent"}, "origin": origin}]}
    record = _checkpointed(checkpoint)
    install_state.validate_v2_record_preimages(tmp_path, record)
    monkeypatch.setattr(install_state, "MAX_TOTAL_PREIMAGE_BYTES", 4)
    with pytest.raises(InstallControlError, match="install_preimages_too_large"):
        install_state.validate_v2_record_preimages(tmp_path, record)
