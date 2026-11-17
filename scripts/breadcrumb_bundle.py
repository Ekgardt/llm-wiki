"""Read one stored breadcrumb bundle back from the private capture runtime."""
from __future__ import annotations

from pathlib import Path

import breadcrumb_protocol as protocol
from bounded_io import check_deadline
from breadcrumb_protocol import BreadcrumbBundle as BreadcrumbBundle
from llm_wiki.formats.canonical import sha256_bytes
from memory_state import MAX_CAPTURE_INTENT_BYTES
from reliable_memory import read_runtime_bytes

# A distinct handler dispatch version; existing session captures keep version 1.
HANDLER_VERSION = 2


def anchor_path(state_root: Path, intent_id: str) -> Path:
    identity = protocol.require_digest(intent_id)
    return state_root / "run/capture-intents/pending" / identity[:2] / f"{identity}.anchor"


def part_path(state_root: Path, intent_id: str, digest: str) -> Path:
    identity = protocol.require_digest(intent_id)
    fingerprint = protocol.require_digest(digest)
    return anchor_path(state_root, identity).with_name(f"{identity}.{fingerprint}.part")


def read(state_root: Path, path: Path) -> bytes:
    return read_runtime_bytes(path, state_root, max_bytes=MAX_CAPTURE_INTENT_BYTES, owner_only=True)


def _read_before_deadline(state_root: Path, path: Path, deadline: float) -> bytes:
    check_deadline(deadline, "breadcrumb verification")
    return read(state_root, path)


def load_bundle(state_root: Path, manifest: bytes, *, deadline: float = float("inf")) -> BreadcrumbBundle:
    record = protocol.read_manifest(manifest)
    intent_id = record["intent_id"]
    anchor = _read_before_deadline(state_root, anchor_path(state_root, intent_id), deadline)
    read_parts: list[bytes] = []

    def read_part(digest: str) -> bytes:
        data = _read_before_deadline(state_root, part_path(state_root, intent_id, digest), deadline)
        read_parts.append(data)
        return data

    content = protocol.restore_input(manifest, anchor, read_part)
    check_deadline(deadline, "breadcrumb verification")
    return BreadcrumbBundle(manifest, anchor, tuple(reversed(read_parts)), content)


def runtime_source_records(state_root: Path, bundle: BreadcrumbBundle) -> tuple[tuple[str, bytes], ...]:
    """Exact auxiliary records; discovery never uses a filename prefix glob."""
    intent_id = protocol.read_manifest(bundle.manifest)["intent_id"]
    anchor = anchor_path(state_root, intent_id)
    records = [(anchor.relative_to(state_root).as_posix(), bundle.anchor)]
    for part in bundle.parts:
        path = part_path(state_root, intent_id, sha256_bytes(part))
        records.append((path.relative_to(state_root).as_posix(), part))
    return tuple(records)


def stored_manifest(state_root: Path, intent_id: str) -> bytes:
    from capture_publication import (
        capture_relative_paths,
    )

    pending, ready = capture_relative_paths(intent_id)
    try:
        return read(state_root, state_root / ready)
    except FileNotFoundError:
        return read(state_root, state_root / pending)


def load_bound_bundle(state_root: Path, intent_id: str, digest: str) -> BreadcrumbBundle:
    manifest = stored_manifest(state_root, intent_id)
    actual = (protocol.read_manifest(manifest)["intent_id"], sha256_bytes(manifest))
    if actual != (intent_id, digest):
        raise ValueError("breadcrumb manifest conflicts with its capture binding")
    return load_bundle(state_root, manifest)
