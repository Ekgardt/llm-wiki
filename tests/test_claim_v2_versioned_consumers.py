"""Versioned literal claims keep strict historical schema and physical authority."""
from copy import deepcopy

import claims
import contradiction_pipeline as contradictions
import pytest
from evidence_resolver import EvidenceResolver
from reliable_memory import canonical_json_bytes, sha256_bytes


def _record(text, version="claim/v2"):
    source = b"# 2026-10-02\n\n## [12:00:00] Source\n" + text.encode() + b"\n"
    start = source.index(text.encode())
    semantic = {"subject": "project", "relation": "has-state", "value": {"type": "string", "value": "available"}, "qualifiers": [], "validity": {"from": "2026-10-02", "to": None}}
    record = {"schema_version": version, "id": "claim-version-test", "fingerprint": sha256_bytes(canonical_json_bytes(semantic)), "text": text, **semantic, "observed_at": "2026-10-02T12:00:00Z", "lifecycle": "active", "confidence": "medium", "authority": "ai-derived", "evidence": {"reference": f"daily:2026-10-02 sha256:{sha256_bytes(source)} block:12:00:00 bytes:{start}-{start+len(text.encode())}", "sha256": sha256_bytes(text.encode()), "text": text}, "links": [], "extractor_version": "test/v2"}
    return record, source


def _page(records, version="claim-ledger/v2"):
    return b"---\ntype: concept\n---\n# Version\n\n## Claims\n```json\n" + claims.claim_json_bytes({"schema_version": version, "claims": records}) + b"\n```\n"


def _candidate(record):
    return {"schema_version": "claim-candidate/v1", "status": "quarantined", "reason": "review", "claim": {**record, "lifecycle": "quarantined"}, "source_page": "knowledge/notes/example.md", "created_at": record["observed_at"]}


def test_long_v2_literal_validates_without_weakening_v1():
    record, _ = _record('{"prompt":"' + "x" * 17_000 + '"}')
    claims.validate_claim_record(record)
    with pytest.raises(ValueError):
        claims.validate_claim_record({**record, "schema_version": "claim/v1"})


def test_mixed_ledger_preserves_historical_records():
    old, _ = _record("old", "claim/v1")
    new, _ = _record("new")
    new["id"] = "claim-new"
    parsed = claims.parse_claim_ledger(_page([old, new]))
    assert parsed["claims"] == [old, new]
    with pytest.raises(ValueError):
        claims.parse_claim_ledger(_page([new], "claim-ledger/v1"))


def test_candidate_dispatch_keeps_historical_wrapper():
    old, _ = _record("old", "claim/v1")
    new, _ = _record("x" * 17_000)
    claims.validate_claim_candidate(_candidate(old))
    claims.validate_claim_candidate(_candidate(new))
    with pytest.raises(ValueError):
        claims.validate_claim_candidate(_candidate({**new, "schema_version": "claim/v1"}))


@pytest.mark.parametrize("version", ["claim/v2", "claim/v99"])
def test_claim_shaped_json_never_becomes_plain_text(version):
    assert contradictions._is_claim_document({"schema_version": version})


def test_v2_normalization_preserves_verified_physical_bytes(tmp_path):
    record, source = _record("e\u0301 " + "x" * 17_000)
    daily = tmp_path / "knowledge/daily/2026-10-02.md"
    daily.parent.mkdir(parents=True)
    daily.write_bytes(source)
    pipeline = claims.ClaimPipeline(EvidenceResolver(tmp_path))
    block = pipeline.split_blocks(source)[0]
    literal = claims.Claim(record, block)
    verified = pipeline.verify_literal(literal)
    normalized = pipeline.normalize(verified)
    assert normalized.record["schema_version"] == "claim/v2"
    assert normalized.record["text"] == record["text"]
    assert normalized.record["evidence"] == record["evidence"]


def test_v2_index_rebuild_retains_exact_literal(tmp_path):
    record, source = _record("x" * 17_000)
    daily = tmp_path / "knowledge/daily/2026-10-02.md"
    page = tmp_path / "knowledge/notes/example.md"
    daily.parent.mkdir(parents=True)
    page.parent.mkdir(parents=True)
    daily.write_bytes(source)
    page.write_bytes(_page([record]))
    index = claims.ClaimIndex(tmp_path, vault=tmp_path)
    index.rebuild([page.parent])
    assert index.active_records()[0].claim.record == record


@pytest.mark.parametrize("field", ["text", "sha256"])
def test_v2_literal_tamper_is_rejected(field):
    record, _ = _record("x" * 17_000)
    damaged = deepcopy(record)
    damaged["evidence"][field] = "bad"
    with pytest.raises(ValueError):
        claims.validate_claim_record(damaged)


@pytest.mark.parametrize("version", ["claim/v1", "claim/v2"])
def test_candidate_must_retain_quarantined_lifecycle(version):
    record, _ = _record("short", version)
    candidate = _candidate(record)
    candidate["claim"]["lifecycle"] = "active"
    with pytest.raises(ValueError):
        claims.validate_claim_candidate(candidate)


def test_v2_exact_unicode_survives_ledger_and_derived_index(tmp_path):
    record, source = _record("e\u0301 " + "x" * 17_000)
    daily = tmp_path / "knowledge/daily/2026-10-02.md"
    page = tmp_path / "knowledge/notes/example.md"
    daily.parent.mkdir(parents=True)
    page.parent.mkdir(parents=True)
    daily.write_bytes(source)
    page.write_bytes(_page([record]))
    assert claims.parse_claim_ledger(page.read_bytes())["claims"][0] == record
    index = claims.ClaimIndex(tmp_path, vault=tmp_path)
    index.rebuild([page.parent])
    assert index.active_records()[0].claim.record == record


def test_v2_envelope_and_operation_serializer_preserve_literal():
    record, _ = _record("e\u0301")
    operation = {"kind": "create", "claims": [record]}
    assert __import__("json").loads(claims.claim_json_bytes(operation)) == operation
    ledger = claims.claim_ledger_document([record])
    assert ledger["schema_version"] == "claim-ledger/v2"
    assert claims.claim_json_bytes(ledger).decode().count("e\u0301") == 2


def test_quarantine_writer_and_lint_preserve_exact_v2_literal(tmp_path, monkeypatch):
    import lint_memory

    record, _ = _record("e\u0301 " + "x" * 17_000)
    pipeline = contradictions.ContradictionPipeline(vault=tmp_path, source_page="knowledge/notes/example.md", evaluators=())
    relative, content, quarantined = pipeline._candidate_file(claims.NormalizedClaim(record))
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)
    assert lint_memory._candidate_record(path, content.decode()) == quarantined
    assert contradictions._embedded_candidate_claim(content) == quarantined


@pytest.mark.parametrize("version", ["claim/v99", None, ["claim/v2"]])
def test_unknown_record_version_refuses_explicitly(version):
    record, _ = _record("short", version)
    with pytest.raises(ValueError):
        claims.validate_claim_record(record)


def test_native_projection_without_claim_keeps_decoded_literal():
    operation = {"kind": "create", "evidence": [{"native_event": "event-owned-selector", "quoted_text": "e\u0301"}]}
    assert __import__("json").loads(claims.claim_json_bytes(operation)) == operation


def test_v2_generation_claim_occurrence_matches_exact_source_bytes():
    from corpus_snapshot import CapturedSource, SourceMetadata, SourceRecord
    from knowledge_extractor import extract_knowledge

    record, _ = _record("e\u0301 " + "x" * 17_000)
    page = _page([record])
    source = CapturedSource(SourceRecord("source:example", "knowledge/notes/example.md", sha256_bytes(page), len(page), "text/markdown", "markdown", None), SourceMetadata("concept"), page)
    result = extract_knowledge((source,))
    occurrence = next(row for row in result.occurrences if row["node_id"].startswith("claim:"))
    assert page[occurrence["byte_start"]:occurrence["byte_end"]] == claims.claim_json_bytes(record)
    assert any(row["edge_type"] == "EVIDENCED_BY" for row in result.assertions)


@pytest.mark.parametrize("version", ["claim/v99", None, ["claim/v2"]])
def test_unknown_claim_document_refuses_before_assessment(monkeypatch, version):
    from unittest.mock import Mock

    record, _ = _record("short", version)
    assess = Mock(side_effect=AssertionError("unknown version must never become a plain query"))
    monkeypatch.setattr(contradictions, "_assess_query_claim", assess)
    with pytest.raises(ValueError, match="claim.*version"):
        contradictions.assess_text(__import__("json").dumps(record))
    assess.assert_not_called()


def test_v2_uses_existing_aggregate_page_budget():
    record, _ = _record("x" * claims.MAX_CLAIM_PAGE_BYTES)
    with pytest.raises(ValueError, match="existing page byte budget"):
        claims.validate_claim_record(record)


def test_v1_encoding_remains_historical_canonical_bytes():
    record, _ = _record("old", "claim/v1")
    assert claims.claim_json_bytes(record) == canonical_json_bytes(record)
    assert claims.claim_json_bytes(_candidate(record)) == canonical_json_bytes(_candidate(record))
    assert claims.claim_ledger_document([record])["schema_version"] == "claim-ledger/v1"
    assert claims.claim_ledger_document([record], previous_version="claim-ledger/v2")["schema_version"] == "claim-ledger/v2"
