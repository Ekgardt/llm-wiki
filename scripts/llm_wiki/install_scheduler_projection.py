"""Persisted scheduler definition bundles and their projections."""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from install_state import (
    MAX_PREIMAGE_BYTES,
    InstallControlError,
    decode_definition_value,
    read_managed_file,
    same_snapshot,
    strict_json_object,
)
from install_state import sha256 as _sha256
from llm_wiki.formats.canonical import canonical_json_bytes


def definition_digest(definitions: Mapping[str, bytes]) -> bytes:
    value = {
        "definitions": {name: _sha256(content) for name, content in definitions.items()},
        "state": "enabled_active",
    }
    return canonical_json_bytes(value)


_DEFINITION_BUNDLE_FORMAT = "scheduler-definitions/v1"


SYSTEMD_DEFINITION_NAMES = (
    "llm-wiki-nightly.service",
    "llm-wiki-nightly.timer",
    "llm-wiki-weekly.service",
    "llm-wiki-weekly.timer",
)


LAUNCHD_DEFINITION_NAMES = (
    "io.github.ekgardt.llm-wiki.nightly.plist",
    "io.github.ekgardt.llm-wiki.weekly.plist",
)


def definition_bundle(definitions: Mapping[str, bytes]) -> bytes:
    value = {
        "definitions": {
            name: base64.b64encode(content).decode("ascii") for name, content in definitions.items()
        },
        "format": _DEFINITION_BUNDLE_FORMAT,
        "state": "enabled_active",
    }
    encoded = canonical_json_bytes(value)
    if len(encoded) > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_definition_bundle_oversized")
    return encoded


def current_definitions_bundle(directory: Path, names: Sequence[str]) -> bytes | None:
    """The scheduler files as they are on disk, bundled like a rendering; None when absent.

    Read for `--adopt` only: an edited unit matches none of our renderings, and this is
    the rollback point that keeps it. A partly present set cannot be restored as one.
    """
    present = _present_definitions(Path(directory), names)
    if not present:
        return None
    if len(present) != len(names):
        raise InstallControlError("install_scheduler_projection_conflict")
    return definition_bundle(present)


def _present_definitions(directory: Path, names: Sequence[str]) -> dict[str, bytes]:
    values = {name: read_managed_file(directory / name) for name in names}
    return {name: value for name, value in values.items() if value is not None}


def _definition_values(value: object, expected_names: Sequence[str]) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise InstallControlError("install_definition_bundle_invalid")
    if set(value) != set(expected_names):
        raise InstallControlError("install_definition_bundle_invalid")
    return value


def _require_definition_total(definitions: Mapping[str, bytes]) -> None:
    if sum(len(content) for content in definitions.values()) > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_definition_bundle_oversized")


def _decode_definition_map(value: object, expected_names: Sequence[str]) -> dict[str, bytes]:
    encoded = _definition_values(value, expected_names)
    definitions = {name: decode_definition_value(encoded[name]) for name in expected_names}
    _require_definition_total(definitions)
    return definitions


def _require_definition_bundle_header(value: Mapping[str, object]) -> None:
    if (
        set(value) != {"definitions", "format", "state"}
        or value.get("format") != _DEFINITION_BUNDLE_FORMAT
        or value.get("state") != "enabled_active"
    ):
        raise InstallControlError("install_definition_bundle_invalid")


def _decode_definition_bundle(candidate: bytes, expected_names: Sequence[str]) -> dict[str, bytes]:
    if len(candidate) > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_definition_bundle_oversized")
    value = strict_json_object(candidate)
    _require_definition_bundle_header(value)
    definitions = _decode_definition_map(value.get("definitions"), expected_names)
    if definition_bundle(definitions) != candidate:
        raise InstallControlError("install_definition_bundle_invalid")
    return definitions


def _require_definition_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise InstallControlError("install_legacy_definition_missing")
    if not directory.is_dir():
        raise InstallControlError("install_legacy_definition_missing")


def _require_definition_entries(directory: Path, expected_names: Sequence[str]) -> None:
    entries = {entry.name for entry in directory.iterdir()}
    if entries != set(expected_names):
        raise InstallControlError("install_legacy_definition_ambiguous")


def _read_definition_values(
    directory: Path, expected_names: Sequence[str]
) -> dict[str, bytes | None]:
    return {name: read_managed_file(directory / name) for name in expected_names}


def _require_present_definitions(values: Mapping[str, bytes | None]) -> None:
    if any(value is None for value in values.values()):
        raise InstallControlError("install_legacy_definition_missing")


def _loaded_definition_values(directory: Path, expected_names: Sequence[str]) -> dict[str, bytes]:
    values = _read_definition_values(directory, expected_names)
    _require_present_definitions(values)
    return {name: value or b"" for name, value in values.items()}


def load_persisted_definitions(directory: Path, expected_names: Sequence[str]) -> dict[str, bytes]:
    _require_definition_directory(directory)
    _require_definition_entries(directory, expected_names)
    return _loaded_definition_values(directory, expected_names)


def definition_candidate(
    candidate: bytes,
    expected_names: Sequence[str],
    legacy_loader: Callable[[], Mapping[str, bytes]],
) -> dict[str, bytes]:
    value = strict_json_object(candidate)
    if value.get("format") == _DEFINITION_BUNDLE_FORMAT:
        return _decode_definition_bundle(candidate, expected_names)
    return _legacy_definitions(candidate, expected_names, legacy_loader)


def _legacy_definitions(
    candidate: bytes,
    expected_names: Sequence[str],
    legacy_loader: Callable[[], Mapping[str, bytes]],
) -> dict[str, bytes]:
    definitions = dict(legacy_loader())
    if set(definitions) != set(expected_names):
        raise InstallControlError("install_legacy_definition_ambiguous")
    if definition_digest(definitions) != candidate:
        raise InstallControlError("install_legacy_definition_mismatch")
    return definitions


def recover_legacy_definitions(
    snapshot: Mapping[str, object],
    expected_names: Sequence[str],
    legacy_loader: Callable[[], Mapping[str, bytes]],
) -> tuple[bytes, bytes]:
    definitions = dict(legacy_loader())
    if set(definitions) != set(expected_names):
        raise InstallControlError("install_legacy_definition_ambiguous")
    bundle = definition_bundle(definitions)
    candidates = (definition_digest(definitions), bundle)
    matches = [candidate for candidate in candidates if same_snapshot(candidate, snapshot)]
    if len(matches) != 1:
        raise InstallControlError("install_legacy_definition_mismatch")
    return matches[0], bundle


def _installed_projection(states: Sequence[tuple[bytes, str]]) -> bytes | None:
    installed = [candidate for candidate, state in states if state == "installed"]
    if not installed:
        return None
    if len(installed) != 1:
        raise InstallControlError("install_scheduler_projection_ambiguous")
    return installed[0]


def _all_scheduler_states_absent(states: Sequence[tuple[bytes, str]]) -> bool:
    return bool(states) and all(state == "absent" for _candidate, state in states)


def _select_scheduler_projection(states: Sequence[tuple[bytes, str]]) -> bytes | None:
    installed = _installed_projection(states)
    if installed is not None:
        return installed
    if _all_scheduler_states_absent(states):
        return None
    raise InstallControlError("install_scheduler_projection_conflict")


def read_scheduler_projections(
    candidates: Sequence[bytes],
    resolver: Callable[[bytes], Mapping[str, bytes]],
    state_reader: Callable[[Mapping[str, bytes]], str],
    absence_reader: Callable[[], str],
) -> bytes | None:
    unique = tuple(dict.fromkeys(candidates))
    if not unique:
        if absence_reader() == "absent":
            return None
        raise InstallControlError("install_scheduler_projection_conflict")
    states = [(candidate, state_reader(resolver(candidate))) for candidate in unique]
    return _select_scheduler_projection(states)


def _projection_candidates(value: bytes | None) -> tuple[bytes, ...]:
    if value is None:
        return ()
    return (value,)


def require_scheduler_projection(
    reader: Callable[[Sequence[bytes]], bytes | None], expected: bytes | None
) -> None:
    if reader(_projection_candidates(expected)) != expected:
        raise InstallControlError("install_scheduler_projection_drift")


def require_projection_before_write(
    reader: Callable[[Sequence[bytes]], bytes | None],
    expected: bytes | None,
    replacement: bytes | None,
) -> None:
    """Where the projection has to start, unless this write is a removal.

    Removal is what a rollback does, and a rollback exists for a resource that
    was left half applied: demanding a readable, single-valued starting
    projection is exactly what makes such a failure impossible to undo. The
    result is still verified after the write, so nothing is accepted unproven.
    """
    if replacement is None:
        return
    require_scheduler_projection(reader, expected)
