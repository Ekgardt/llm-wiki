from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from decimal import Decimal
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


def test_numeric_values_normalize_decimal_strings_without_binary_floats(pipeline) -> None:
    block = pipeline.split_blocks(source_bytes())[0]
    exponent = raw_claim()
    exponent["claims"][0]["value"] = {
        "type": "number",
        "value": "1.2300e2",
        "unit": " Seconds ",
    }
    normalized = pipeline.normalize(pipeline.verify_literal(pipeline.extract(block, exponent)[0]))
    assert normalized.record["value"] == {
        "type": "number",
        "value": "123",
        "unit": "seconds",
    }

    canonical = raw_claim()
    canonical["claims"][0]["value"] = {
        "type": "number",
        "value": Decimal("123"),
        "unit": "seconds",
    }
    same = pipeline.normalize(pipeline.verify_literal(pipeline.extract(block, canonical)[0]))
    assert same.record["fingerprint"] == normalized.record["fingerprint"]

    for invalid in (1.25, float("nan"), float("inf"), "NaN", "Infinity"):
        record = raw_claim()
        record["claims"][0]["value"] = {
            "type": "number",
            "value": invalid,
            "unit": "seconds",
        }
        with pytest.raises(ValueError, match="number"):
            pipeline.extract(block, record)


@pytest.mark.parametrize(
    "value",
    ["9" * 129, "1e129", "1e-129", "1e999999999999999999999999999999"],
)
def test_decimal_bounds_reject_before_decimal_construction(
    value: str, monkeypatch
) -> None:
    import claims

    class DecimalMustNotRun:
        def __new__(cls, _value):
            raise AssertionError("Decimal constructed before lexical bounds")

    monkeypatch.setattr(claims, "Decimal", DecimalMustNotRun)

    with pytest.raises(ValueError, match="number"):
        claims._canonical_decimal(value)


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


def test_observed_at_is_real_rfc3339_and_matches_valid_timestamp_block(pipeline) -> None:
    from claims import validate_claim_record

    normalized = pipeline.normalize(
        pipeline.verify_literal(pipeline.extract(pipeline.split_blocks(source_bytes())[0], raw_claim())[0])
    )
    for observed in (
        "2026-02-30T03:04:05Z",
        "2026-01-02T24:00:00Z",
        "2026-01-02T03:04:05+00:00",
        "2026-01-02T04:05:06Z",
    ):
        changed = json.loads(json.dumps(normalized.record))
        changed["observed_at"] = observed
        with pytest.raises(ValueError, match="observ"):
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
    index.rebuild([page.parent])

    assert index.path == state / "cache/claims.sqlite3"
    with sqlite3.connect(index.path) as database:
        assert database.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert database.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert len(index.candidates(normalized, limit=1)) == 1
    assert index.candidates(normalized, limit=0) == []
    page.write_bytes(b"---\ntype: concept\n---\n# Ledgerless\n")
    index.rebuild([page.parent])
    assert index.candidates(normalized) == []


def test_claim_index_rejects_escape_symlink_oversize_and_unbounded_limit(
    pipeline, tmp_path: Path
) -> None:
    from claims import ClaimIndex

    index = ClaimIndex(tmp_path / "state")
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="canonical knowledge roots"):
        index.rebuild([outside])
    with pytest.raises((PermissionError, ValueError)):
        index.rebuild(lambda: [outside])
    with pytest.raises(ValueError, match="limit"):
        index.candidates(None, limit=51)
    huge = tmp_path / "knowledge/notes/huge.md"
    huge.parent.mkdir(parents=True)
    huge.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="exceeds"):
        index.rebuild(lambda: [huge])
    if hasattr(os, "symlink"):
        link = huge.with_name("link.md")
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable")
        with pytest.raises(PermissionError):
            index.rebuild(lambda: [link])


def test_claim_index_serializes_scan_and_publish_so_stale_rebuild_cannot_win(
    pipeline, tmp_path: Path
) -> None:
    from claims import ClaimIndex

    normalized = pipeline.normalize(
        pipeline.verify_literal(pipeline.extract(pipeline.split_blocks(source_bytes())[0], raw_claim())[0])
    )
    old_page = tmp_path / "knowledge/notes/old.md"
    new_page = tmp_path / "knowledge/notes/new.md"
    old_page.parent.mkdir(parents=True)
    old_page.write_bytes(ledger_page(normalized.record))
    newer = json.loads(json.dumps(normalized.record))
    newer["id"] = "claim:newer:0"
    new_page.write_bytes(ledger_page(newer))
    state = tmp_path / "state"
    stale = ClaimIndex(state)
    fresh = ClaimIndex(state)
    entered = threading.Event()
    release = threading.Event()
    original = stale._page_bytes

    def slow_read(page: Path):
        entered.set()
        assert release.wait(5)
        return original(page)

    stale._page_bytes = slow_read
    stale_thread = threading.Thread(target=stale.rebuild, args=(lambda: [old_page],))
    fresh_thread = threading.Thread(target=fresh.rebuild, args=(lambda: [new_page],))
    stale_thread.start()
    assert entered.wait(5)
    fresh_thread.start()
    time.sleep(0.2)
    assert fresh_thread.is_alive()
    release.set()
    stale_thread.join(5)
    fresh_thread.join(5)
    assert not stale_thread.is_alive() and not fresh_thread.is_alive()
    assert [item.page for item in fresh.candidates(normalized)] == [
        "knowledge/notes/new.md"
    ]


def test_claim_index_evaluates_page_provider_only_after_rebuild_lock(
    pipeline, tmp_path: Path
) -> None:
    from claims import ClaimIndex, _exclusive_file_lock

    normalized = pipeline.normalize(
        pipeline.verify_literal(pipeline.extract(pipeline.split_blocks(source_bytes())[0], raw_claim())[0])
    )
    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    old_page = notes / "old.md"
    new_page = notes / "new.md"
    old_page.write_bytes(ledger_page(normalized.record))
    index = ClaimIndex(tmp_path / "state")
    provider_called = threading.Event()
    errors: list[BaseException] = []

    def provider() -> list[Path]:
        provider_called.set()
        return sorted(notes.glob("*.md"))

    def rebuild() -> None:
        try:
            index.rebuild(provider)
        except BaseException as exc:
            errors.append(exc)

    index.path.parent.mkdir(parents=True)
    with _exclusive_file_lock(index.lock_path):
        worker = threading.Thread(target=rebuild)
        worker.start()
        time.sleep(0.2)
        assert worker.is_alive()
        assert not provider_called.is_set()
        newer = json.loads(json.dumps(normalized.record))
        newer["id"] = "claim:newer:0"
        new_page.write_bytes(ledger_page(newer))
    worker.join(5)

    assert not worker.is_alive()
    assert errors == []
    assert provider_called.is_set()
    assert [item.page for item in index.candidates(normalized)] == [
        "knowledge/notes/new.md",
        "knowledge/notes/old.md",
    ]


def test_claim_rebuild_lock_timeout_does_not_unlock_the_owner(tmp_path: Path) -> None:
    from claims import _exclusive_file_lock

    lock = tmp_path / "claims.rebuild.lock"
    failures: list[BaseException] = []

    def contend() -> None:
        try:
            with _exclusive_file_lock(lock, timeout=0.1):
                raise AssertionError("contender acquired an owned lock")
        except BaseException as exc:
            failures.append(exc)

    with _exclusive_file_lock(lock):
        contender = threading.Thread(target=contend)
        contender.start()
        contender.join(2)
        assert not contender.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], TimeoutError)


def test_claim_index_rebuild_replaces_incompatible_disposable_schema(
    pipeline, tmp_path: Path
) -> None:
    from claims import CLAIM_INDEX_SCHEMA_VERSION, ClaimIndex

    normalized = pipeline.normalize(
        pipeline.verify_literal(pipeline.extract(pipeline.split_blocks(source_bytes())[0], raw_claim())[0])
    )
    page = tmp_path / "knowledge/notes/service.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(ledger_page(normalized.record))
    state = tmp_path / "state"
    database_path = state / "cache/claims.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as database:
        database.execute("CREATE TABLE claim(obsolete TEXT)")
        database.execute("CREATE TABLE claim_index_meta(schema_version TEXT)")
        database.execute("INSERT INTO claim_index_meta VALUES ('claim-index/obsolete')")

    index = ClaimIndex(state)
    index.rebuild(lambda: [page])

    with sqlite3.connect(database_path) as database:
        columns = [row[1] for row in database.execute("PRAGMA table_info(claim)")]
        version = database.execute(
            "SELECT schema_version FROM claim_index_meta"
        ).fetchone()[0]
    assert columns == [
        "id",
        "fingerprint",
        "subject",
        "relation",
        "lifecycle",
        "page",
        "record_json",
    ]
    assert version == CLAIM_INDEX_SCHEMA_VERSION
    assert len(index.candidates(normalized)) == 1


def test_claim_index_rebuild_replaces_shape_compatible_but_untrusted_schema(
    pipeline, tmp_path: Path
) -> None:
    from claims import CLAIM_INDEX_SCHEMA_VERSION, ClaimIndex

    normalized = pipeline.normalize(
        pipeline.verify_literal(pipeline.extract(pipeline.split_blocks(source_bytes())[0], raw_claim())[0])
    )
    page = tmp_path / "knowledge/notes/service.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(ledger_page(normalized.record))
    state = tmp_path / "state"
    database_path = state / "cache/claims.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as database:
        database.execute(
            "CREATE TABLE claim(id, fingerprint, subject, relation, lifecycle, page, record_json)"
        )
        database.execute("CREATE TABLE claim_index_meta(schema_version TEXT)")
        database.execute(
            "INSERT INTO claim_index_meta VALUES (?)", (CLAIM_INDEX_SCHEMA_VERSION,)
        )

    ClaimIndex(state).rebuild(lambda: [page])

    with sqlite3.connect(database_path) as database:
        columns = [tuple(row[1:6]) for row in database.execute("PRAGMA table_info(claim)")]
        indexed = [
            row[2]
            for row in database.execute("PRAGMA index_info(claim_candidate_lookup)")
        ]
    assert columns == [
        ("id", "TEXT", 1, None, 2),
        ("fingerprint", "TEXT", 1, None, 0),
        ("subject", "TEXT", 1, None, 0),
        ("relation", "TEXT", 1, None, 0),
        ("lifecycle", "TEXT", 1, None, 0),
        ("page", "TEXT", 1, None, 1),
        ("record_json", "BLOB", 1, None, 0),
    ]
    assert indexed == ["subject", "relation", "lifecycle", "fingerprint"]


def test_claim_index_failed_publication_rolls_back_previous_snapshot(
    pipeline, tmp_path: Path
) -> None:
    from claims import ClaimIndex

    normalized = pipeline.normalize(
        pipeline.verify_literal(pipeline.extract(pipeline.split_blocks(source_bytes())[0], raw_claim())[0])
    )
    page = tmp_path / "knowledge/notes/service.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(ledger_page(normalized.record))
    index = ClaimIndex(tmp_path / "state")
    index.rebuild(lambda: [page])
    duplicate_ledger = {
        "schema_version": "claim-ledger/v1",
        "claims": [normalized.record, normalized.record],
    }
    encoded = json.dumps(
        duplicate_ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    page.write_text(
        f"---\ntype: concept\n---\n# Service\n\n## Claims\n```json\n{encoded}\n```\n",
        encoding="utf-8",
    )

    with pytest.raises(sqlite3.IntegrityError):
        index.rebuild(lambda: [page])

    assert len(index.candidates(normalized)) == 1


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

    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)

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


def test_lint_skips_claim_json_in_prose_scan_but_resolves_claim_evidence(
    pipeline, tmp_path: Path, monkeypatch
) -> None:
    import lint_memory

    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)
    normalized = pipeline.normalize(
        pipeline.verify_literal(pipeline.extract(pipeline.split_blocks(source_bytes())[0], raw_claim())[0])
    )
    page = tmp_path / "knowledge/notes/claim.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(ledger_page(normalized.record))

    assert lint_memory.check_evidence_references([page]) == []
    assert lint_memory.check_claim_schemas([page]) == []

    malformed = json.loads(json.dumps(normalized.record))
    malformed["evidence"]["reference"] = malformed["evidence"]["reference"].replace(
        malformed["evidence"]["reference"].split("sha256:", 1)[1][:64], "0" * 64
    )
    page.write_bytes(ledger_page(malformed))
    findings = lint_memory.check_claim_schemas([page])
    assert len(findings) == 1
    assert "hash mismatch" in findings[0]

    page.write_text(
        "## Evidence\n- `daily:2026-01-02 broken`\n\n## Claims\nnot-json\n",
        encoding="utf-8",
    )
    assert lint_memory.check_evidence_references([page])
    assert lint_memory.check_claim_schemas([page])


def test_lint_candidate_location_and_project_claim_page_selection(
    pipeline, tmp_path: Path, monkeypatch
) -> None:
    import lint_memory

    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)
    claims_dir = tmp_path / "knowledge/inbox/claims"
    wrong_dir = tmp_path / "knowledge/inbox/review"
    project = tmp_path / "knowledge/projects/demo"
    claims_dir.mkdir(parents=True)
    wrong_dir.mkdir(parents=True)
    project.mkdir(parents=True)
    for name in ("state.md", "context.md", "journal.md", "other.md"):
        (project / name).write_bytes(
            b"---\ntype: project-state\n---\n# X\n\n## Claims\nnot-json\n"
        )
    normalized = pipeline.normalize(
        pipeline.verify_literal(pipeline.extract(pipeline.split_blocks(source_bytes())[0], raw_claim())[0])
    )
    candidate_claim = json.loads(json.dumps(normalized.record))
    candidate_claim["lifecycle"] = "quarantined"
    candidate_record = {
        "schema_version": "claim-candidate/v1",
        "status": "quarantined",
        "reason": "review",
        "claim": candidate_claim,
        "source_page": "knowledge/notes/service.md",
        "created_at": "2026-01-02T03:04:05Z",
    }
    encoded = json.dumps(
        candidate_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    candidate = (
        f"---\ntype: claim-candidate\n---\n# Candidate\n\n```json\n{encoded}\n```\n"
    )
    allowed = claims_dir / "allowed.md"
    misplaced = wrong_dir / "misplaced.md"
    allowed.write_text(candidate, encoding="utf-8")
    misplaced.write_text(candidate, encoding="utf-8")
    assert lint_memory.check_claim_schemas([allowed]) == []
    assert any(
        "only under knowledge/inbox/claims" in item
        for item in lint_memory.check_claim_schemas([misplaced])
    )
    selected = lint_memory._project_claim_pages(tmp_path / "knowledge/projects")
    assert [item.name for item in selected] == ["context.md", "journal.md", "state.md"]
    assert len(lint_memory.check_claim_schemas(selected)) == 3
