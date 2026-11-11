"""Canonical permanent breadcrumb documents, independent of publication."""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path, PurePosixPath

import breadcrumb_protocol as protocol
from bounded_io import MAX_KNOWLEDGE_PAGE_BYTES
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes

SOURCE_VERSION = "breadcrumb-source/v1"
RECORD_MARKER = b"## Integrity record\n\n    "
FRONTMATTER = (
    "---\ntype: raw-source\nsource_authority: session\nconfidence: medium\n"
    "record_schema: breadcrumb-source/v1\n---\n\n"
)


def source_path(anchor: bytes) -> str:
    record = protocol.read_anchor(anchor)
    return f"knowledge/raw/sessions/{record['day']}/{record['intent_id']}.breadcrumb.md"


def source_part_path(anchor: bytes, digest: str) -> str:
    fingerprint = protocol.require_digest(digest)
    # The full part digest already binds the occurrence. Repeating both hashes
    # exceeds the existing Markdown transaction component limit (128 bytes).
    name = f"{fingerprint}.breadcrumb-part.md"
    return str(PurePosixPath(source_path(anchor)).with_name(name))


def is_breadcrumb_document(path: Path) -> bool:
    """Reserved full-digest filenames are integrity records, not sessions."""
    return re.fullmatch(r"[0-9a-f]{64}\.breadcrumb(?:-part)?\.md", path.name) is not None


def archived_source_path(relative: str) -> str:
    path = PurePosixPath(relative)
    if len(path.parts) != 5 or path.parts[:3] != ("knowledge", "raw", "sessions"):
        raise ValueError("breadcrumb source has no canonical archive location")
    day = path.parent.name
    require_exact(date.fromisoformat(day).isoformat(), day)
    return f"knowledge/raw/sessions/archive/{day[:7]}/{day}/{path.name}"


def bounded_document(data: bytes) -> bytes:
    if len(data) > MAX_KNOWLEDGE_PAGE_BYTES:
        raise ValueError("breadcrumb source exceeds the common Markdown reader bound")
    return data


def document(title: str, links: str, record: bytes) -> bytes:
    prefix = f"{FRONTMATTER}# {title}\n\n{links}\n\n"
    return bounded_document(prefix.encode() + RECORD_MARKER + record + b"\n")


def embedded_record(document: bytes) -> bytes:
    bounded_document(document)
    _prefix, marker, record = document.partition(RECORD_MARKER)
    if not marker or not record.endswith(b"\n"):
        raise ValueError("breadcrumb source has no complete integrity record")
    return record[:-1]


def require_exact(actual: object, expected: object) -> None:
    if actual != expected:
        raise ValueError("breadcrumb permanent evidence changed or conflicts")


def render_head(manifest: bytes, anchor: bytes) -> bytes:
    record = protocol.read_manifest(manifest)
    occurrence = protocol.read_anchor(anchor)
    require_exact(record["anchor_sha256"], sha256_bytes(anchor))
    require_exact(record["intent_id"], occurrence["intent_id"])
    last = PurePosixPath(source_part_path(anchor, record["last_part_sha256"])).name
    links = f"Source: captured event `{record['intent_id']}`.\n\n[Last part]({last})"
    payload = canonical_json_bytes({
        "schema_version": SOURCE_VERSION, "manifest": record, "anchor": occurrence,
    })
    return document("Complete captured event", links, payload)


def render_part(anchor: bytes, part: bytes) -> bytes:
    record = protocol.read_part(part)
    occurrence = protocol.read_anchor(anchor)
    require_exact(record["intent_id"], occurrence["intent_id"])
    head = PurePosixPath(source_path(anchor)).name
    links = f"Source: [complete captured event]({head})."
    if record["previous_sha256"] is not None:
        previous = PurePosixPath(source_part_path(anchor, record["previous_sha256"])).name
        links += f"\n\n[Previous part]({previous})"
    return document(f"Captured event — part {record['index'] + 1}", links, part)


def source_documents(manifest: bytes, anchor: bytes, parts: tuple[bytes, ...]) -> Iterator[tuple[str, bytes]]:
    """Yield verified parts first and the head last; never truncate the source."""
    by_digest = {sha256_bytes(part): part for part in parts}
    protocol.restore_input(manifest, anchor, by_digest.__getitem__)
    require_exact(len(parts), protocol.read_manifest(manifest)["part_count"])
    for part in parts:
        digest = sha256_bytes(part)
        yield source_part_path(anchor, digest), render_part(anchor, part)
    yield source_path(anchor), render_head(manifest, anchor)


def read_head(document: bytes) -> tuple[bytes, bytes]:
    record = json.loads(embedded_record(document).decode("utf-8", errors="strict"))
    require_exact(set(record), {"schema_version", "manifest", "anchor"})
    require_exact(record["schema_version"], SOURCE_VERSION)
    manifest = canonical_json_bytes(record["manifest"])
    anchor = canonical_json_bytes(record["anchor"])
    require_exact(document, render_head(manifest, anchor))
    return manifest, anchor


def restore_source(head_relative: str, read_document: Callable[[str], bytes]) -> bytes:
    """Verify permanent evidence independently of disposable runtime parts.

    The caller must provide a contained, stable, bounded Markdown reader. Every
    requested part path is regenerated from the validated occurrence and digest.
    """
    manifest, anchor = read_head(read_document(head_relative))
    require_exact(head_relative, source_path(anchor))

    def read_part(digest: str) -> bytes:
        document = read_document(source_part_path(anchor, digest))
        part = embedded_record(document)
        require_exact(document, render_part(anchor, part))
        return part

    return protocol.restore_input(manifest, anchor, read_part)
