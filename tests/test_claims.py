from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_bytes() -> bytes:
    return (
        "# 2026-01-02\n"
        "## [03:04:05] session\n"
        "Service A DEPENDS-ON Café.\n"
        "## [04:05:06] session\n"
        "Second block.\n"
    ).encode()


def raw_claim(source: bytes | None = None) -> dict[str, object]:
    source = source or source_bytes()
    literal = "Service A DEPENDS-ON Café.".encode()
    start = source.index(literal)
    return {
        "schema_version": "claim-extraction/v1",
        "claims": [
            {
                "id": "claim:2026-01-02-03:04:05:0",
                "text": literal.decode(),
                "subject": " Service A ",
                "relation": "DEPENDS-ON",
                "value": {"type": "entity", "value": " Café "},
                "qualifiers": [
                    {"key": " Environment ", "value": {"type": "string", "value": " PROD "}}
                ],
                "validity": {"from": None, "to": None},
                "lifecycle": "active",
                "confidence": "high",
                "authority": "user",
                "evidence": {
                    "reference": (
                        f"daily:2026-01-02 sha256:{sha(source)} block:03:04:05 "
                        f"bytes:{start}-{start + len(literal)}"
                    ),
                    "sha256": sha(literal),
                    "text": literal.decode(),
                },
                "links": [],
                "extractor_version": "extractor/v1",
            }
        ],
    }


@pytest.fixture
def pipeline(tmp_path: Path):
    from claims import ClaimPipeline
    from evidence_resolver import EvidenceResolver

    daily = tmp_path / "knowledge/daily/2026-01-02.md"
    daily.parent.mkdir(parents=True)
    daily.write_bytes(source_bytes())
    return ClaimPipeline(EvidenceResolver(tmp_path))


def test_pipeline_order_stops_before_normalization_on_bad_literal(pipeline) -> None:
    from claims import EvidenceMismatch

    block = pipeline.split_blocks(source_bytes())[0]
    extracted = pipeline.extract(block, raw_claim())[0]
    extracted.record["evidence"]["sha256"] = "0" * 64
    with pytest.raises(EvidenceMismatch, match="hash"):
        pipeline.verify_literal(extracted)
    assert pipeline.calls == ["split_blocks", "extract", "verify_evidence"]


def test_split_blocks_preserves_exact_immutable_utf8_ranges(pipeline) -> None:
    source = source_bytes()
    blocks = pipeline.split_blocks(source)
    assert [(block.block_id, block.observed_at) for block in blocks] == [
        ("03:04:05", "2026-01-02T03:04:05Z"),
        ("04:05:06", "2026-01-02T04:05:06Z"),
    ]
    assert b"Caf\xc3\xa9" in blocks[0].bytes
    assert source[blocks[0].byte_start : blocks[0].byte_end] == blocks[0].bytes
    with pytest.raises(ValueError, match="UTF-8"):
        pipeline.split_blocks(b"# 2026-01-02\n## [03:04:05] x\n\xff")
    with pytest.raises(ValueError, match="timestamp"):
        pipeline.split_blocks(b"# 2026-01-02\n## [not-time] x\ntext")


def test_extract_is_strict_and_binds_claim_to_its_block(pipeline) -> None:
    block = pipeline.split_blocks(source_bytes())[0]
    claim = pipeline.extract(block, raw_claim())[0]
    assert claim.block is block
    malformed = raw_claim()
    malformed["claims"][0]["unknown"] = True
    with pytest.raises(ValueError, match="schema"):
        pipeline.extract(block, malformed)


def test_verify_literal_rejects_ambiguous_wrong_text_and_wrong_block(pipeline) -> None:
    from claims import EvidenceMismatch

    block = pipeline.split_blocks(source_bytes())[0]
    for mutate in ("text", "block"):
        record = raw_claim()
        if mutate == "text":
            record["claims"][0]["evidence"]["text"] = "Service A depends on Cafe."
        else:
            record["claims"][0]["evidence"]["reference"] = record["claims"][0]["evidence"][
                "reference"
            ].replace("block:03:04:05", "block:04:05:06")
        claim = pipeline.extract(block, record)[0]
        with pytest.raises(EvidenceMismatch):
            pipeline.verify_literal(claim)


def test_normalize_is_deterministic_unicode_canonical_and_fingerprinted(pipeline) -> None:
    block = pipeline.split_blocks(source_bytes())[0]
    verified = pipeline.verify_literal(pipeline.extract(block, raw_claim())[0])
    normalized = pipeline.normalize(verified)
    assert normalized.record["id"] == "claim:2026-01-02-03:04:05:0"
    assert normalized.record["subject"] == "service a"
    assert normalized.record["relation"] == "depends-on"
    assert normalized.record["value"] == {"type": "entity", "value": "café"}
    assert normalized.record["qualifiers"] == [
        {"key": "environment", "value": {"type": "string", "value": "PROD"}}
    ]
    assert normalized.record["observed_at"] == "2026-01-02T03:04:05Z"
    assert normalized.record["fingerprint"] == pipeline.normalize(verified).record["fingerprint"]
    assert len(normalized.record["fingerprint"]) == 64


def test_validated_record_binds_fingerprint_observation_and_literal_hash(pipeline) -> None:
    from claims import validate_claim_record

    normalized = pipeline.normalize(
        pipeline.verify_literal(pipeline.extract(pipeline.split_blocks(source_bytes())[0], raw_claim())[0])
    )
    for field in ("fingerprint", "observed_at", "evidence"):
        changed = json.loads(json.dumps(normalized.record))
        if field == "evidence":
            changed["evidence"]["sha256"] = "0" * 64
        elif field == "observed_at":
            changed[field] = "2026-01-02T04:05:06Z"
        else:
            changed[field] = "0" * 64
        with pytest.raises(ValueError):
            validate_claim_record(changed)


def ledger_page(claim: dict[str, object]) -> bytes:
    ledger = {"schema_version": "claim-ledger/v1", "claims": [claim]}
    encoded = json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"---\ntype: concept\n---\n# Service\n\n## Claims\n```json\n{encoded}\n```\n".encode()


def test_claim_index_rebuilds_derived_delete_full_database_and_bounds_candidates(
    pipeline, tmp_path: Path
) -> None:
    from claims import ClaimIndex

    normalized = pipeline.normalize(
        pipeline.verify_literal(pipeline.extract(pipeline.split_blocks(source_bytes())[0], raw_claim())[0])
    )
    page = tmp_path / "knowledge/notes/service.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(ledger_page(normalized.record))
    state = tmp_path / "state"
    index = ClaimIndex(state)
    index.rebuild([page])

    assert index.path == state / "cache/claims.sqlite3"
    with sqlite3.connect(index.path) as database:
        assert database.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert database.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert len(index.candidates(normalized, limit=1)) == 1
    assert index.candidates(normalized, limit=0) == []
    page.write_bytes(b"---\ntype: concept\n---\n# Ledgerless\n")
    index.rebuild([page])
    assert index.candidates(normalized) == []


def test_claim_index_rejects_escape_symlink_oversize_and_unbounded_limit(
    pipeline, tmp_path: Path
) -> None:
    from claims import ClaimIndex

    index = ClaimIndex(tmp_path / "state")
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises((PermissionError, ValueError)):
        index.rebuild([outside])
    with pytest.raises(ValueError, match="limit"):
        index.candidates(None, limit=51)
    huge = tmp_path / "knowledge/notes/huge.md"
    huge.parent.mkdir(parents=True)
    huge.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="exceeds"):
        index.rebuild([huge])
    if hasattr(os, "symlink"):
        link = huge.with_name("link.md")
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable")
        with pytest.raises(PermissionError):
            index.rebuild([link])


def test_substantive_and_ledgerless_policy() -> None:
    from claims import is_substantive, page_may_auto_supersede

    item = raw_claim()["claims"][0]
    assert is_substantive(item)
    for relation in ("title", "summary", "link", "provenance", "mentions"):
        changed = dict(item, relation=relation)
        assert not is_substantive(changed)
    assert not is_substantive(dict(item, lifecycle="quarantined"))
    malformed_evidence = json.loads(json.dumps(item))
    malformed_evidence["evidence"]["sha256"] = "0" * 64
    assert not is_substantive(malformed_evidence)
    assert not page_may_auto_supersede(None)
    assert page_may_auto_supersede({"schema_version": "claim-ledger/v1", "claims": [item]})


def test_lint_validates_claim_ledgers_and_candidates(tmp_path: Path, monkeypatch) -> None:
    import lint_memory

    page = tmp_path / "knowledge/notes/page.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: concept\n---\n# X\n\n## Claims\n```json\n{\"claims\":[],\"schema_version\":\"claim-ledger/v1\"}\n```\n",
        encoding="utf-8",
    )
    assert lint_memory.check_claim_schemas([page]) == []
    page.write_text(page.read_text(encoding="utf-8").replace('"claims":[]', '"claims":[],"x":1'), encoding="utf-8")
    assert lint_memory.check_claim_schemas([page])

    candidate = tmp_path / "knowledge/inbox/claims/candidate.md"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("---\ntype: claim-candidate\n---\n# C\n", encoding="utf-8")
    assert lint_memory.check_claim_schemas([candidate])
