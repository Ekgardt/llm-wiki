from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest
from install_state import (
    InstallControlError,
    ManagedResource,
    file_resource,
    resource_snapshot,
)
from llm_wiki import install_v2_transactions as v2
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.install_records import ResourceDriftError, error_code, prepare_install_root

from tests.install_control_helpers import _release

_Record = dict[str, object]
_ABSENT: _Record = {"state": "absent"}


def _resource(
    current: bytes | None,
    *,
    projections: Callable[[Sequence[bytes]], bytes | None] | None = None,
    legacy: Callable[[Mapping[str, object]], tuple[bytes, bytes]] | None = None,
) -> ManagedResource:
    return ManagedResource(
        resource_id="thing",
        kind="config_file",
        locator="/managed/thing",
        desired=b"wanted",
        read_owned=lambda: current,
        write_owned=lambda _value: None,
        recognizes=lambda _value: True,
        read_projections=projections,
        recover_legacy_projection=legacy,
    )


def _projection_failing(code: str) -> Callable[[Sequence[bytes]], bytes | None]:
    def read(_candidates: Sequence[bytes]) -> bytes | None:
        raise InstallControlError(code)

    return read


def _config(tmp_path: Path, desired: bytes = b"hello") -> ManagedResource:
    return file_resource(
        resource_id="cfg", kind="config_file", path=tmp_path / "config", desired=desired
    )


def _install(
    tmp_path: Path,
    desired: bytes = b"hello",
    *,
    control_version: int,
    retiring: Sequence[ManagedResource] = (),
    released: Sequence[ManagedResource] = (),
) -> _Record:
    return v2.install_resources(
        state_root=tmp_path / "state",
        vault_root=tmp_path,
        release=_release(),
        scheduler_backend="cron",
        resources=[_config(tmp_path, desired)],
        control_version=control_version,
        retiring=retiring,
        released=released,
    )


def _install_root(tmp_path: Path) -> Path:
    return tmp_path / "state" / "run" / "install"


def _edit_transaction(tmp_path: Path, **changes: object) -> None:
    path = _install_root(tmp_path) / "transaction.json"
    current: _Record = json.loads(path.read_bytes())
    path.write_bytes(canonical_json_bytes({**current, **changes}))


def _refused(code: str, action: Callable[[], object]) -> None:
    with pytest.raises(InstallControlError, match=code):
        action()


def test_a_half_applied_schedule_reverting_to_absence_starts_unreadable(tmp_path: Path) -> None:
    resource = _resource(
        None, projections=_projection_failing("install_scheduler_projection_conflict")
    )
    current = v2._revert_start_projection(tmp_path, resource, (_ABSENT,), _ABSENT)
    assert isinstance(current, v2._UnreadableProjection)
    assert not v2._revert_already_done(current, _ABSENT)
    v2._require_revertible(current, resource_snapshot(b"wanted"))


@pytest.mark.parametrize(
    ("code", "rollback"),
    [
        ("install_scheduler_projection_conflict", resource_snapshot(b"before")),
        ("install_scheduler_output_oversized", _ABSENT),
    ],
    ids=["reverting-to-content", "another-failure"],
)
def test_an_unreadable_projection_is_refused_unless_it_reverts_to_absence(
    tmp_path: Path, code: str, rollback: _Record
) -> None:
    resource = _resource(None, projections=_projection_failing(code))
    with pytest.raises(InstallControlError) as raised:
        v2._revert_start_projection(tmp_path, resource, (_ABSENT,), rollback)
    assert error_code(raised.value) == code


def test_a_record_without_metadata_cannot_be_reverted() -> None:
    _refused("install_transaction_invalid", lambda: v2._resource_record_metadata({"metadata": []}))


def test_a_revert_that_does_not_reach_its_rollback_point_is_refused(tmp_path: Path) -> None:
    _refused(
        "install_rollback_verification_failed",
        lambda: v2._require_rolled_back(tmp_path, _resource(b"still here"), _ABSENT),
    )


@pytest.mark.parametrize(
    ("current", "expected"), [(None, "0" * 64), (b"other", "0" * 64)], ids=["gone", "changed"]
)
def test_a_manifest_that_moved_under_an_update_is_refused(
    current: bytes | None, expected: str
) -> None:
    _refused("install_manifest_changed", lambda: v2._require_manifest_cas(current, expected))


def test_a_first_install_over_an_existing_manifest_is_a_conflict() -> None:
    _refused("install_manifest_conflict", lambda: v2._require_manifest_cas(b"manifest", None))


def test_a_handed_over_file_that_already_exists_is_not_ours(tmp_path: Path) -> None:
    present = file_resource(
        resource_id="drop-in", kind="config_file", path=tmp_path / "drop-in", desired=b"unit"
    )
    (tmp_path / "drop-in").write_bytes(b"someone's")
    _refused(
        "install_resource_ownership_ambiguous",
        lambda: _install(tmp_path, control_version=2, released=[present]),
    )


def test_a_preimage_whose_bytes_changed_is_drift(tmp_path: Path) -> None:
    _refused(
        "install_resource_drift",
        lambda: v2._normalized_snapshot(tmp_path, resource_snapshot(b"recorded"), b"changed"),
    )


def test_an_absent_checkpoint_stays_absent(tmp_path: Path) -> None:
    assert v2._checkpoint_desired_snapshot(tmp_path, _ABSENT, _resource(b"x")) == _ABSENT


def test_an_owned_resource_that_moved_since_its_checkpoint_is_named(tmp_path: Path) -> None:
    with pytest.raises(ResourceDriftError, match="install_resource_drift"):
        v2._owned_snapshot(tmp_path, resource_snapshot(b"recorded"), _resource(b"moved"))


def test_a_legacy_schedule_that_moved_since_its_checkpoint_is_named(tmp_path: Path) -> None:
    resource = _resource(b"x", legacy=lambda _snapshot: (b"moved", b"persisted"))
    with pytest.raises(ResourceDriftError, match="install_resource_drift"):
        v2._checkpoint_owned_snapshot(tmp_path, resource_snapshot(b"recorded"), resource)


def test_a_checkpoint_record_without_an_origin_is_invalid(tmp_path: Path) -> None:
    record: _Record = {"desired": _ABSENT, "origin": None}
    _refused(
        "install_state_schema_invalid",
        lambda: v2._checkpoint_resource(tmp_path, record, _resource(None)),
    )


def test_the_same_request_again_returns_the_installed_manifest(tmp_path: Path) -> None:
    manifest = _install(tmp_path, control_version=2)
    assert _install(tmp_path, control_version=2) == manifest


def test_a_quarantined_install_blocks_an_update(tmp_path: Path) -> None:
    _install(tmp_path, control_version=2)
    _edit_transaction(tmp_path, state="quarantined")
    _refused(
        "install_transaction_blocks_new_work",
        lambda: _install(tmp_path, b"changed", control_version=2),
    )


def test_an_unfinished_transaction_blocks_a_different_request(tmp_path: Path) -> None:
    _install(tmp_path, control_version=2)
    _edit_transaction(tmp_path, state="mutating")
    _refused(
        "install_transaction_blocks_new_work",
        lambda: _install(tmp_path, b"changed", control_version=2),
    )


def test_an_unsettled_transaction_without_a_manifest_blocks_a_new_install(
    tmp_path: Path,
) -> None:
    _install(tmp_path, control_version=2)
    (_install_root(tmp_path) / "manifest.json").unlink()
    _edit_transaction(tmp_path, state="quarantined")
    _refused("install_transaction_blocks_new_work", lambda: _install(tmp_path, control_version=2))


def test_a_transaction_resumed_over_another_base_manifest_is_refused() -> None:
    _refused(
        "install_manifest_transaction_mismatch",
        lambda: v2.require_v2_base_manifest({"generation": 1}, {"base_manifest_sha256": None}),
    )


def test_a_publication_without_its_manifest_cannot_complete(tmp_path: Path) -> None:
    install_root = prepare_install_root(tmp_path)
    _refused(
        "install_manifest_transaction_mismatch",
        lambda: v2.complete_v2_publication(install_root / "transaction.json", {}, None),
    )


def test_retiring_resources_need_the_second_format(tmp_path: Path) -> None:
    _refused(
        "install_control_version_invalid",
        lambda: _install(tmp_path, control_version=1, retiring=[_config(tmp_path)]),
    )


def test_an_unknown_control_version_is_refused(tmp_path: Path) -> None:
    _refused("install_control_version_invalid", lambda: _install(tmp_path, control_version=3))
    assert not (tmp_path / "state").exists()
