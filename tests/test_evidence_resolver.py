from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from reliable_memory import canonical_json_bytes  # noqa: E402


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reference(daily_id: str, source: bytes, block: str, start: int, end: int) -> str:
    return (
        f"daily:{daily_id} sha256:{_sha(source)} block:{block} "
        f"bytes:{start}-{end}"
    )


def _write_bag(root: Path, daily_id: str, source: bytes, *, suffix: str = "one") -> Path:
    bag = root / "knowledge" / "daily" / "archive" / daily_id[:7] / f"bag-test-{suffix}"
    payload_name = f"data/{daily_id}.md"
    (bag / "data").mkdir(parents=True)
    (bag / payload_name).write_bytes(source)
    (bag / "bagit.txt").write_bytes(
        b"BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n"
    )
    (bag / "bag-info.txt").write_bytes(
        f"External-Identifier: daily:{daily_id}\n".encode()
    )
    (bag / "manifest-sha256.txt").write_bytes(
        f"{_sha(source)}  {payload_name}\n".encode()
    )
    block_start = source.index(b"## [")
    evidence = {
        "block_id": "evt-1",
        "byte_start": block_start,
        "byte_end": len(source),
        "line_start": 2,
        "line_end": 4,
        "sha256": _sha(source[block_start:]),
    }
    manifest = {
        "schema_version": "archive-manifest/v1",
        "logical_daily_id": daily_id,
        "original_path": f"knowledge/daily/{daily_id}.md",
        "source_hash": _sha(source),
        "payload_hash": _sha(source),
        "compile_receipt_ref": {
            "schema": "compile-receipt-ref/v1",
            "path": f"knowledge/daily/receipts/{_sha(source)}.md",
            "source_digest": _sha(source),
            "receipt_file_hash": "a" * 64,
        },
        "queue_preflight": {
            "checked_at": "2026-07-14T00:00:00Z",
            "passed": True,
            "blocking_task_ids": [],
        },
        "operations": [{"operation_id": "compile:test", "state": "succeeded"}],
        "evidence": [evidence],
        "pins": [],
        "retention_days": 90,
    }
    (bag / "archive-manifest.json").write_bytes(canonical_json_bytes(manifest))
    tags = ("archive-manifest.json", "bag-info.txt", "bagit.txt", "manifest-sha256.txt")
    (bag / "tagmanifest-sha256.txt").write_bytes(
        "".join(f"{_sha((bag / name).read_bytes())}  {name}\n" for name in tags).encode()
    )
    return bag


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    (tmp_path / "knowledge" / "daily" / "archive").mkdir(parents=True)
    return tmp_path


def test_parse_requires_exact_canonical_logical_reference() -> None:
    from evidence_resolver import EvidenceRef

    value = f"daily:2026-01-01 sha256:{'a' * 64} block:evt-1 bytes:10-20"
    assert str(EvidenceRef.parse(value)) == value
    for invalid in (
        value + " trailing",
        value.replace("2026-01-01", "../secret"),
        value.replace("bytes:10-20", "bytes:20-10"),
        value.replace("block:evt-1", "block:../evt"),
        value.replace("sha256:", "sha256:A"),
    ):
        with pytest.raises(ValueError):
            EvidenceRef.parse(invalid)


def test_flat_resolution_uses_utf8_half_open_bytes_lines_and_block_hash(vault: Path) -> None:
    from evidence_resolver import EvidenceRef, EvidenceResolver

    source = "# day\n## [evt-1] event\nαβ line\nnext\n".encode()
    path = vault / "knowledge" / "daily" / "2026-01-01.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(source)
    start = source.index("α".encode())
    end = start + len("αβ line".encode())

    result = EvidenceResolver(vault).resolve(
        EvidenceRef.parse(_reference("2026-01-01", source, "evt-1", start, end))
    )

    block = source[source.index(b"## [evt-1]") :]
    assert result.bytes == "αβ line".encode()
    assert result.sha256 == _sha(result.bytes)
    assert result.source_sha256 == _sha(source)
    assert result.block_sha256 == _sha(block)
    assert (result.byte_start, result.byte_end) == (start, end)
    assert (result.line_start, result.line_end) == (3, 4)
    assert result.location == "flat"


def test_resolution_rejects_non_utf8_boundary_outside_or_ambiguous_block(vault: Path) -> None:
    from evidence_resolver import EvidenceRef, EvidenceResolutionError, EvidenceResolver

    source = "## [evt-1] first\nα\n## [evt-1] second\nα\n".encode()
    path = vault / "knowledge" / "daily" / "2026-01-01.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(source)
    alpha = source.index("α".encode())
    resolver = EvidenceResolver(vault)

    with pytest.raises(EvidenceResolutionError, match="ambiguous"):
        resolver.resolve(EvidenceRef.parse(_reference("2026-01-01", source, "evt-1", alpha, alpha + 2)))

    unique = b"preamble\n## [evt-1] event\n" + "α".encode() + b"\n"
    path.write_bytes(unique)
    alpha = unique.index("α".encode())
    with pytest.raises(EvidenceResolutionError, match="UTF-8"):
        resolver.resolve(EvidenceRef.parse(_reference("2026-01-01", unique, "evt-1", alpha + 1, alpha + 2)))
    with pytest.raises(EvidenceResolutionError, match="block"):
        resolver.resolve(EvidenceRef.parse(_reference("2026-01-01", unique, "evt-1", 0, 2)))


def test_flat_hash_mismatch_fails_closed_without_archive_fallback(vault: Path) -> None:
    from evidence_resolver import EvidenceRef, EvidenceResolutionError, EvidenceResolver

    archived = b"# day\n## [evt-1] event\narchived\n"
    _write_bag(vault, "2026-01-01", archived)
    flat = vault / "knowledge" / "daily" / "2026-01-01.md"
    flat.write_bytes(b"# day\n## [evt-1] event\nchanged\n")
    start = archived.index(b"archived")

    with pytest.raises(EvidenceResolutionError, match="hash mismatch"):
        EvidenceResolver(vault).resolve(
            EvidenceRef.parse(_reference("2026-01-01", archived, "evt-1", start, start + 8))
        )


def test_validated_archive_resolves_identically_and_ambiguity_fails(vault: Path) -> None:
    from evidence_resolver import EvidenceRef, EvidenceResolutionError, EvidenceResolver

    source = b"# day\n## [evt-1] event\narchived bytes\n"
    _write_bag(vault, "2026-01-01", source)
    start = source.index(b"archived bytes")
    ref = EvidenceRef.parse(_reference("2026-01-01", source, "evt-1", start, len(source) - 1))
    result = EvidenceResolver(vault).resolve(ref)
    assert result.bytes == b"archived bytes"
    assert result.location == "archive"

    _write_bag(vault, "2026-01-01", source, suffix="two")
    with pytest.raises(EvidenceResolutionError, match="ambiguous"):
        EvidenceResolver(vault).resolve(ref)


def test_archive_validation_rejects_tamper_oversize_and_links(vault: Path) -> None:
    from evidence_resolver import EvidenceRef, EvidenceResolutionError, EvidenceResolver

    source = b"# day\n## [evt-1] event\narchived bytes\n"
    bag = _write_bag(vault, "2026-01-01", source)
    start = source.index(b"archived bytes")
    ref = EvidenceRef.parse(_reference("2026-01-01", source, "evt-1", start, len(source) - 1))
    (bag / "archive-manifest.json").write_bytes(b"{" + b" " * 1_100_000 + b"}")
    with pytest.raises(EvidenceResolutionError, match="exceeds"):
        EvidenceResolver(vault).resolve(ref)

    if hasattr(os, "symlink"):
        for child in bag.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink()
        outside = vault / "outside.md"
        outside.write_bytes(source)
        try:
            os.symlink(outside, bag / "archive-manifest.json")
        except OSError:
            pytest.skip("links require privileges on this platform")
        with pytest.raises(EvidenceResolutionError, match="non-symlink|regular"):
            EvidenceResolver(vault).resolve(ref)
