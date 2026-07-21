"""Complete normalized code-intelligence contract tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace

import pytest
from code_intelligence import (
    AnalysisIdentity,
    AnalysisOutcome,
    AnalysisRun,
    AnalysisScope,
    AnalyzerReceipt,
    Capability,
    Coverage,
    CoverageStatus,
    Diagnostic,
    DiagnosticSeverity,
    EvidenceLevel,
    NormalizedAnalysis,
    PositionEncoding,
    PositionRange,
    RelatedLocation,
    Relationship,
    RelationshipClaim,
    RelationshipResolution,
    SubjectKind,
    SymbolClaim,
    SymbolIdentity,
    SymbolRole,
    Validity,
    ValidityStatus,
    VerifiedAnalysisBatch,
    closed_world,
    verify_native_analysis,
)
from corpus_snapshot import CorpusSnapshot, SnapshotPolicy, SourceMetadata, SourceRecord

from tests.code_kernel_helpers import (
    make_analysis_identity,
    make_analysis_scope,
    make_normalized_analysis,
    make_run,
)

SHA = tuple(character * 64 for character in "0123456789abcdef")


@pytest.fixture
def snapshot() -> CorpusSnapshot:
    from corpus_snapshot import CapturedSource

    records = (
        SourceRecord("source:a", "a.py", SHA[1], 4, "text/x-python", "python", None),
        SourceRecord("source:b", "b.py", SHA[2], 4, "text/x-python", "python", None),
    )
    sources = tuple(
        CapturedSource(record, SourceMetadata(type="code"), b"name") for record in records
    )
    policy = SnapshotPolicy((), (".",), False, None, 10, 100, 1000, 100, 100, 10)
    return CorpusSnapshot(sources, (), SHA[0], policy)


def identity(**changes: object) -> AnalysisIdentity:
    values: dict[str, object] = {
        "source_manifest_sha256": SHA[0],
        "manifest_sha256": SHA[1],
        "lockfile_sha256": SHA[2],
        "sdk_sha256": SHA[3],
        "target_sha256": SHA[4],
        "configuration_sha256": SHA[5],
        "feature_sha256": SHA[6],
        "invocation_sha256": SHA[7],
        "environment_sha256": SHA[8],
        "dependency_state_sha256": SHA[9],
        "position_encoding": PositionEncoding.UTF8,
    }
    values.update(changes)
    return AnalysisIdentity.create(**values)  # type: ignore[arg-type]


def scope(**changes: object) -> AnalysisScope:
    values: dict[str, object] = {
        "scope_id": "scope:" + SHA[1],
        "run_id": "run:" + SHA[2],
        "source_manifest_sha256": SHA[0],
        "build_target": "default",
        "build_configuration": "default",
        "expected_source_ids": ("source:a", "source:b"),
        "generated_sources": "available",
        "dependency_resolution": "complete",
        "analyzer_support": "complete",
    }
    values.update(changes)
    return AnalysisScope(**values)  # type: ignore[arg-type]


def run(**changes: object) -> AnalysisRun:
    current_identity = changes.pop("identity", identity())
    values: dict[str, object] = {
        "run_id": "run:" + SHA[2],
        "identity": current_identity,
        "source_manifest_sha256": current_identity.source_manifest_sha256,
        "analysis_mode": "native-syntax",
        "repository_id": "repository:test",
        "checkout_id": "checkout:test",
        "source_generation_id": "generation:test",
        "analyzer_family": "cpython-ast-symtable",
        "analyzer_version": "3.10",
        "protocol": "native",
        "protocol_version": "1",
        "executable_sha256": SHA[10],
        "declared_capabilities": (Capability.DEFINITIONS,),
        "evidence_level": EvidenceLevel.SYNTAX,
        "qualified": True,
        "outcome": AnalysisOutcome.COMPLETE,
        "receipt_sha256": None,
        "receipt_output_sha256": None,
        "consent_grant_id": None,
        "consent_revision": None,
        "lease_id": None,
        "started_at": "2026-07-21T00:00:00Z",
        "ended_at": "2026-07-21T00:00:01Z",
    }
    values.update(changes)
    return AnalysisRun(**values)  # type: ignore[arg-type]


def symbol(**changes: object) -> SymbolClaim:
    values: dict[str, object] = {
        "claim_id": "claim:symbol",
        "run_id": "run:" + SHA[2],
        "scope_id": "scope:" + SHA[1],
        "source_id": "source:a",
        "capability": Capability.DEFINITIONS,
        "identity": SymbolIdentity("scip", "symbol:test"),
        "display_name": "name",
        "symbol_kind": "function",
        "role": SymbolRole.DEFINITION,
        "range": PositionRange(0, 4),
        "evidence_level": EvidenceLevel.SYNTAX,
        "ambiguity": False,
    }
    values.update(changes)
    return SymbolClaim(**values)  # type: ignore[arg-type]


def relationship(**changes: object) -> RelationshipClaim:
    values: dict[str, object] = {
        "claim_id": "claim:relationship",
        "run_id": "run:" + SHA[2],
        "scope_id": "scope:" + SHA[1],
        "source_id": "source:a",
        "source_identity": SymbolIdentity("scip", "symbol:test"),
        "relation": Relationship.REFERENCES_SYMBOL,
        "capability": Capability.REFERENCES,
        "target_identity": SymbolIdentity("scip", "symbol:target"),
        "target_text": None,
        "resolution": RelationshipResolution.RESOLVED,
        "range": PositionRange(0, 4),
        "evidence_level": EvidenceLevel.SYNTAX,
        "ambiguity": False,
    }
    values.update(changes)
    return RelationshipClaim(**values)  # type: ignore[arg-type]


def diagnostic(**changes: object) -> Diagnostic:
    values: dict[str, object] = {
        "diagnostic_id": "diagnostic:test",
        "run_id": "run:" + SHA[2],
        "scope_id": "scope:" + SHA[1],
        "source_id": "source:a",
        "capability": Capability.DIAGNOSTICS,
        "severity": DiagnosticSeverity.WARNING,
        "code": "W1",
        "message": "warning",
        "range": PositionRange(0, 4),
        "evidence_level": EvidenceLevel.SYNTAX,
        "related": (),
    }
    values.update(changes)
    return Diagnostic(**values)  # type: ignore[arg-type]


def normalized(**changes: object) -> NormalizedAnalysis:
    values: dict[str, object] = {
        "run": run(),
        "scopes": (scope(),),
        "coverage": (
            Coverage(
                "scope:" + SHA[1],
                "source:a",
                Capability.DEFINITIONS,
                CoverageStatus.COMPLETE,
                True,
                None,
            ),
            Coverage(
                "scope:" + SHA[1],
                "source:b",
                Capability.DEFINITIONS,
                CoverageStatus.COMPLETE,
                True,
                None,
            ),
        ),
        "symbols": (symbol(),),
        "relationships": (),
        "diagnostics": (),
        "validity": (
            Validity(
                "validity:symbol",
                SubjectKind.SYMBOL,
                "claim:symbol",
                ValidityStatus.CURRENT,
                None,
            ),
        ),
        "receipt": None,
    }
    values.update(changes)
    return NormalizedAnalysis(**values)  # type: ignore[arg-type]


def normalized_with_relationship(claim_range: PositionRange) -> NormalizedAnalysis:
    current_run = run(
        declared_capabilities=(Capability.DEFINITIONS, Capability.REFERENCES)
    )
    rows = tuple(
        Coverage(
            "scope:" + SHA[1],
            source_id,
            capability,
            CoverageStatus.COMPLETE,
            True,
            None,
        )
        for source_id in ("source:a", "source:b")
        for capability in (Capability.DEFINITIONS, Capability.REFERENCES)
    )
    relation = relationship(range=claim_range)
    return normalized(
        run=current_run,
        coverage=rows,
        relationships=(relation,),
        validity=(
            Validity(
                "validity:relationship",
                SubjectKind.RELATIONSHIP,
                relation.claim_id,
                ValidityStatus.CURRENT,
                None,
            ),
            Validity(
                "validity:symbol",
                SubjectKind.SYMBOL,
                "claim:symbol",
                ValidityStatus.CURRENT,
                None,
            ),
        ),
    )


def normalized_with_diagnostic(
    claim_range: PositionRange = PositionRange(0, 4),
    related: tuple[RelatedLocation, ...] = (),
) -> NormalizedAnalysis:
    current_run = run(
        declared_capabilities=(Capability.DEFINITIONS, Capability.DIAGNOSTICS)
    )
    rows = tuple(
        Coverage(
            "scope:" + SHA[1],
            source_id,
            capability,
            CoverageStatus.COMPLETE,
            True,
            None,
        )
        for source_id in ("source:a", "source:b")
        for capability in (Capability.DEFINITIONS, Capability.DIAGNOSTICS)
    )
    item = diagnostic(range=claim_range, related=related)
    return normalized(
        run=current_run,
        coverage=rows,
        diagnostics=(item,),
        validity=(
            Validity(
                "validity:diagnostic",
                SubjectKind.DIAGNOSTIC,
                item.diagnostic_id,
                ValidityStatus.CURRENT,
                None,
            ),
            Validity(
                "validity:symbol",
                SubjectKind.SYMBOL,
                "claim:symbol",
                ValidityStatus.CURRENT,
                None,
            ),
        ),
    )


def test_closed_enums_have_exact_values() -> None:
    expected = {
        Capability: {
            "definitions", "declarations", "references", "calls", "imports", "types",
            "type_definitions", "inheritance", "implementations", "diagnostics",
        },
        PositionEncoding: {"utf-8", "utf-16", "utf-32"},
        CoverageStatus: {
            "complete", "partial", "failed", "cancelled", "rejected", "unsupported", "excluded",
        },
        EvidenceLevel: {"compiler", "semantic", "syntax", "lexical"},
        AnalysisOutcome: {"complete", "partial", "failed", "cancelled", "rejected", "superseded"},
        ValidityStatus: {"current", "soft-stale", "hard-stale"},
        DiagnosticSeverity: {"error", "warning", "information", "hint"},
        Relationship: {
            "REFERENCES_SYMBOL", "CALLS", "IMPORTS", "HAS_TYPE", "TYPE_DEFINITION",
            "INHERITS", "IMPLEMENTS",
        },
        RelationshipResolution: {"resolved", "unresolved", "ambiguous"},
        SubjectKind: {"symbol", "relationship", "diagnostic"},
    }
    for enum_type, values in expected.items():
        assert {item.value for item in enum_type} == values


def test_all_records_are_frozen_and_slotted() -> None:
    item = PositionRange(0, 1)
    assert "__dict__" not in dir(item)
    with pytest.raises(FrozenInstanceError):
        item.byte_start = 1  # type: ignore[misc]


def test_analysis_identity_has_exact_fields_and_is_change_sensitive() -> None:
    base = identity()
    assert tuple(base.as_dict()) == tuple(item.name for item in fields(AnalysisIdentity))
    assert tuple(base.as_dict()) == (
        "source_manifest_sha256", "manifest_sha256", "lockfile_sha256", "sdk_sha256",
        "target_sha256", "configuration_sha256", "feature_sha256", "invocation_sha256",
        "environment_sha256", "dependency_state_sha256", "position_encoding", "analysis_sha256",
    )
    assert base.recompute_analysis_sha256() == base.analysis_sha256
    for name in tuple(base.as_dict())[:-2]:
        assert replace(base, **{name: SHA[15]}).recompute_analysis_sha256() != base.analysis_sha256
    changed_encoding = replace(base, position_encoding=PositionEncoding.UTF16)
    assert changed_encoding.recompute_analysis_sha256() != base.analysis_sha256


@pytest.mark.parametrize("bad", ["A" * 64, "0" * 63, "g" * 64, 1, None])
def test_sha256_components_are_lowercase_and_exact(bad: object) -> None:
    with pytest.raises((TypeError, ValueError), match="sha256"):
        identity(lockfile_sha256=bad)


def test_run_rejects_an_identity_with_stale_analysis_hash() -> None:
    stale = replace(identity(), lockfile_sha256=SHA[15])
    with pytest.raises(ValueError, match="analysis_sha256"):
        run(identity=stale)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, True),
        ({"generated_sources": "not-required"}, True),
        ({"generated_sources": "unavailable"}, False),
        ({"dependency_resolution": "partial"}, False),
        ({"dependency_resolution": "unavailable"}, False),
        ({"analyzer_support": "partial"}, False),
        ({"analyzer_support": "unsupported"}, False),
        ({"analyzer_support": "unqualified"}, False),
    ],
)
def test_closed_world_requires_all_scope_states(changes: dict[str, object], expected: bool) -> None:
    current = scope(**changes)
    rows = tuple(
        Coverage(current.scope_id, source_id, Capability.REFERENCES, CoverageStatus.COMPLETE, True, None)
        for source_id in current.expected_source_ids
    )
    assert closed_world(current, rows, Capability.REFERENCES) is expected


@pytest.mark.parametrize("damage", ["missing", "duplicate", "extra", "partial", "ineligible"])
def test_incomplete_duplicate_or_wrong_coverage_is_not_closed_world(damage: str) -> None:
    current = scope()
    rows = [
        Coverage(current.scope_id, "source:a", Capability.REFERENCES, CoverageStatus.COMPLETE, True, None),
        Coverage(current.scope_id, "source:b", Capability.REFERENCES, CoverageStatus.COMPLETE, True, None),
    ]
    if damage == "missing":
        rows.pop()
    elif damage == "duplicate":
        rows.append(rows[0])
    elif damage == "extra":
        rows.append(Coverage(current.scope_id, "source:c", Capability.REFERENCES, CoverageStatus.COMPLETE, True, None))
    elif damage == "partial":
        rows[0] = replace(rows[0], status=CoverageStatus.PARTIAL, closed_world_eligible=False, reason="partial")
    else:
        rows[0] = replace(rows[0], closed_world_eligible=False)
    assert closed_world(current, tuple(rows), Capability.REFERENCES) is False


def test_closed_world_ignores_neither_wrong_scope_nor_capability() -> None:
    current = scope()
    rows = (
        Coverage("scope:other", "source:a", Capability.REFERENCES, CoverageStatus.COMPLETE, True, None),
        Coverage(current.scope_id, "source:b", Capability.CALLS, CoverageStatus.COMPLETE, True, None),
    )
    assert closed_world(current, rows, Capability.REFERENCES) is False


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_source_ids": ["source:a"]},
        {"expected_source_ids": ("source:b", "source:a")},
        {"expected_source_ids": ("source:a", "source:a")},
        {"generated_sources": "sometimes"},
        {"dependency_resolution": "unknown"},
        {"analyzer_support": "unknown"},
        {"build_target": "x" * 257},
        {"build_configuration": "bad\ud800"},
    ],
)
def test_scope_requires_canonical_bounded_values(changes: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        scope(**changes)


@pytest.mark.parametrize("start,end", [(-1, 0), (0, -1), (True, 1), (0, False), (2, 1)])
def test_position_range_is_nonnegative_half_open(start: object, end: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        PositionRange(start, end)  # type: ignore[arg-type]


def test_position_range_nonempty_boundary() -> None:
    assert PositionRange(0, 0).byte_start == 0
    with pytest.raises(ValueError, match="claim.*non-empty"):
        PositionRange(0, 0).require_nonempty("claim range")
    assert PositionRange(0, 1).require_nonempty("claim range") == PositionRange(0, 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": CoverageStatus.COMPLETE, "reason": "unexpected"},
        {"status": CoverageStatus.PARTIAL, "reason": None},
        {"status": CoverageStatus.PARTIAL, "closed_world_eligible": True, "reason": "partial"},
        {"closed_world_eligible": 1},
        {"reason": "x" * 1025, "status": CoverageStatus.PARTIAL, "closed_world_eligible": False},
    ],
)
def test_coverage_enforces_terminal_reason_and_boolean_invariants(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "scope_id": "scope:test", "source_id": "source:a",
        "capability": Capability.REFERENCES, "status": CoverageStatus.COMPLETE,
        "closed_world_eligible": True, "reason": None,
    }
    values.update(changes)
    with pytest.raises((TypeError, ValueError)):
        Coverage(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"source_manifest_sha256": SHA[15]},
        {"declared_capabilities": (Capability.REFERENCES, Capability.DEFINITIONS)},
        {"declared_capabilities": (Capability.DEFINITIONS, Capability.DEFINITIONS)},
        {"qualified": 1},
        {"consent_revision": True, "analysis_mode": "precise"},
        {"analyzer_family": "x" * 257},
        {"protocol": "bad\ud800"},
    ],
)
def test_run_validates_identity_canonical_values_and_bool_int_boundary(changes: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        run(**changes)


def test_run_mode_invariants() -> None:
    with pytest.raises(ValueError, match="precise"):
        run(analysis_mode="precise", evidence_level=EvidenceLevel.COMPILER)
    precise = run(
        analysis_mode="precise", evidence_level=EvidenceLevel.COMPILER,
        receipt_sha256=SHA[11], receipt_output_sha256=SHA[12],
        consent_grant_id="grant:test", consent_revision=1, lease_id="lease:test",
    )
    assert precise.consent_revision == 1
    with pytest.raises(ValueError, match="native-syntax"):
        run(receipt_sha256=SHA[11])
    with pytest.raises(ValueError, match="evidence"):
        run(evidence_level=EvidenceLevel.COMPILER)


@pytest.mark.parametrize("revision", [0, -1, True, 1.5])
def test_precise_consent_revision_is_positive_integer(revision: object) -> None:
    with pytest.raises((TypeError, ValueError), match="consent_revision"):
        run(
            analysis_mode="precise", evidence_level=EvidenceLevel.COMPILER,
            receipt_sha256=SHA[11], receipt_output_sha256=SHA[12],
            consent_grant_id="grant:test", consent_revision=revision, lease_id="lease:test",
        )


def test_symbol_claim_validates_role_capability_range_and_ids() -> None:
    with pytest.raises(ValueError, match="capability"):
        symbol(capability=Capability.REFERENCES)
    with pytest.raises(ValueError, match="role"):
        symbol(role=SymbolRole.DECLARATION)
    with pytest.raises(ValueError, match="non-empty"):
        symbol(range=PositionRange(0, 0))
    with pytest.raises(ValueError, match="claim_id"):
        symbol(claim_id="")
    with pytest.raises(TypeError, match="ambiguity"):
        symbol(ambiguity=1)


def test_relationship_target_resolution_and_capability_are_consistent() -> None:
    assert relationship().target_identity is not None
    assert relationship(
        target_identity=None, target_text="name", resolution=RelationshipResolution.UNRESOLVED
    ).target_text == "name"
    assert relationship(
        target_identity=None, target_text="name", resolution=RelationshipResolution.AMBIGUOUS,
        ambiguity=True,
    ).ambiguity
    with pytest.raises(ValueError, match="target"):
        relationship(target_identity=None)
    with pytest.raises(ValueError, match="target"):
        relationship(target_text="also")
    with pytest.raises(ValueError, match="target"):
        relationship(resolution=RelationshipResolution.UNRESOLVED)
    with pytest.raises(ValueError, match="ambiguity"):
        relationship(
            target_identity=None, target_text="name", resolution=RelationshipResolution.AMBIGUOUS
        )
    with pytest.raises(ValueError, match="capability"):
        relationship(capability=Capability.CALLS)


def test_diagnostic_validates_sorted_unique_related_and_evidence() -> None:
    first = RelatedLocation("source:a", PositionRange(0, 1), "a")
    second = RelatedLocation("source:b", PositionRange(0, 1), None)
    assert diagnostic(related=(first, second)).related == (first, second)
    with pytest.raises(ValueError, match="sorted"):
        diagnostic(related=(second, first))
    with pytest.raises(ValueError, match="unique"):
        diagnostic(related=(first, first))
    with pytest.raises(ValueError, match="evidence"):
        diagnostic(evidence_level=EvidenceLevel.LEXICAL)
    with pytest.raises(ValueError, match="capability"):
        diagnostic(capability=Capability.REFERENCES)


def test_validity_subject_and_stale_reason_are_consistent() -> None:
    assert Validity(
        "validity:1", SubjectKind.SYMBOL, "claim:1", ValidityStatus.CURRENT, None
    ).stale_reason is None
    with pytest.raises(ValueError, match="stale_reason"):
        Validity(
            "validity:1", SubjectKind.SYMBOL, "claim:1", ValidityStatus.CURRENT, "changed"
        )
    with pytest.raises(ValueError, match="stale_reason"):
        Validity(
            "validity:1",
            SubjectKind.RELATIONSHIP,
            "claim:1",
            ValidityStatus.SOFT_STALE,
            None,
        )
    with pytest.raises(TypeError, match="subject_kind"):
        Validity("validity:1", "run", "claim:1", ValidityStatus.CURRENT, None)  # type: ignore[arg-type]


def test_receipt_hashes_must_match_run_identity() -> None:
    current_run = run(
        analysis_mode="precise", evidence_level=EvidenceLevel.COMPILER,
        receipt_sha256=SHA[11], receipt_output_sha256=SHA[12],
        consent_grant_id="grant:test", consent_revision=1, lease_id="lease:test",
    )
    receipt = AnalyzerReceipt(
        receipt_sha256=SHA[11], output_sha256=SHA[12],
        source_manifest_sha256=current_run.identity.source_manifest_sha256,
        analysis_sha256=current_run.identity.analysis_sha256,
    )
    assert receipt.analysis_sha256 == current_run.identity.analysis_sha256
    with pytest.raises(ValueError, match="receipt"):
        normalized(run=current_run, receipt=replace(receipt, receipt_sha256=SHA[13]))
    with pytest.raises(ValueError, match="analysis_sha256"):
        normalized(run=current_run, receipt=replace(receipt, analysis_sha256=SHA[13]))


def test_normalized_analysis_enforces_one_sorted_unique_universe() -> None:
    base = normalized()
    assert base.all_claims() == base.symbols + base.relationships + base.diagnostics
    with pytest.raises(TypeError, match="symbols"):
        normalized(symbols=(object(),))
    with pytest.raises(ValueError, match="sorted"):
        normalized(scopes=(scope(scope_id="scope:z"), scope(scope_id="scope:a")))
    with pytest.raises(ValueError, match="unique"):
        normalized(symbols=(symbol(), symbol()))
    with pytest.raises(ValueError, match="run_id"):
        normalized(symbols=(symbol(run_id="run:other"),))
    with pytest.raises(ValueError, match="scope_id"):
        normalized(relationships=(relationship(scope_id="scope:other"),))
    with pytest.raises(ValueError, match="source_id"):
        normalized(diagnostics=(diagnostic(source_id="source:other"),))
    with pytest.raises(ValueError, match="coverage"):
        normalized(coverage=())


def test_normalized_analysis_rejects_unknown_related_source() -> None:
    with pytest.raises(ValueError, match="related.*source_id"):
        normalized_with_diagnostic(
            related=(RelatedLocation("source:unknown", PositionRange(0, 1), None),)
        )


def test_normalized_analysis_rejects_duplicate_build_scope() -> None:
    first = scope(scope_id="scope:" + SHA[1])
    second = scope(scope_id="scope:" + SHA[2])
    rows = tuple(
        Coverage(
            current_scope.scope_id,
            source_id,
            Capability.DEFINITIONS,
            CoverageStatus.COMPLETE,
            True,
            None,
        )
        for current_scope in (first, second)
        for source_id in current_scope.expected_source_ids
    )
    with pytest.raises(ValueError, match="build_target.*build_configuration"):
        normalized(
            scopes=(first, second),
            coverage=rows,
            symbols=(),
            validity=(),
        )


def test_helper_round_trips(snapshot: CorpusSnapshot) -> None:
    current_scope = make_analysis_scope(snapshot)
    current_identity = make_analysis_identity(snapshot, current_scope)
    current_run = make_run(snapshot)
    analysis = make_normalized_analysis(snapshot, current_scope)
    assert current_scope.expected_source_ids == ("source:a", "source:b")
    assert current_identity.source_manifest_sha256 == snapshot.corpus_sha256
    assert current_run.identity.source_manifest_sha256 == snapshot.corpus_sha256
    assert analysis.run.run_id == current_scope.run_id
    assert make_run(snapshot, outcome="partial").outcome is AnalysisOutcome.PARTIAL


def test_verified_batch_cannot_be_constructed_with_an_ordinary_mint() -> None:
    analysis = normalized()
    with pytest.raises(TypeError, match="internal verifiers"):
        VerifiedAnalysisBatch(
            analysis, analysis_mode="native-syntax",
            source_manifest_sha256=analysis.run.identity.source_manifest_sha256,
            analysis_sha256=analysis.run.identity.analysis_sha256,
            receipt_sha256=None, consent_grant_id=None, consent_revision=None, lease_id=None,
            _mint=object(),
        )


def test_verify_native_analysis_accepts_valid_fixture(snapshot: CorpusSnapshot) -> None:
    analysis = make_normalized_analysis(snapshot, make_analysis_scope(snapshot))
    batch = verify_native_analysis(snapshot, analysis)
    assert batch.analysis is analysis
    assert batch.analysis_mode == "native-syntax"
    assert batch.source_manifest_sha256 == snapshot.corpus_sha256
    assert batch.analysis_sha256 == analysis.run.identity.analysis_sha256
    assert batch.receipt_sha256 is batch.consent_grant_id is batch.consent_revision is batch.lease_id is None


def test_verify_native_analysis_rejects_uncaptured_expected_source(
    snapshot: CorpusSnapshot,
) -> None:
    analysis = make_normalized_analysis(snapshot, make_analysis_scope(snapshot))
    incomplete_snapshot = replace(snapshot, sources=snapshot.sources[:1])
    with pytest.raises(ValueError, match="source:b.*captured"):
        verify_native_analysis(incomplete_snapshot, analysis)


@pytest.mark.parametrize("kind", ["symbol", "relationship", "diagnostic", "related"])
def test_verify_native_analysis_rejects_ranges_beyond_captured_bytes(
    snapshot: CorpusSnapshot,
    kind: str,
) -> None:
    out_of_bounds = PositionRange(0, 5)
    if kind == "symbol":
        analysis = normalized(symbols=(symbol(range=out_of_bounds),))
    elif kind == "relationship":
        analysis = normalized_with_relationship(out_of_bounds)
    elif kind == "diagnostic":
        analysis = normalized_with_diagnostic(claim_range=out_of_bounds)
    else:
        analysis = normalized_with_diagnostic(
            related=(RelatedLocation("source:b", out_of_bounds, None),)
        )
    with pytest.raises(ValueError, match=f"{kind}.*range.*captured bytes"):
        verify_native_analysis(snapshot, analysis)


def test_verify_native_analysis_rejects_lexical_run_evidence(
    snapshot: CorpusSnapshot,
) -> None:
    analysis = make_normalized_analysis(snapshot, make_analysis_scope(snapshot))
    lexical_run = replace(analysis.run, evidence_level=EvidenceLevel.LEXICAL)
    with pytest.raises(ValueError, match="run.*syntax evidence"):
        verify_native_analysis(snapshot, replace(analysis, run=lexical_run))


def test_verify_native_analysis_rejects_manifest_evidence_and_mode_mismatch(
    snapshot: CorpusSnapshot,
) -> None:
    analysis = make_normalized_analysis(snapshot, make_analysis_scope(snapshot))
    wrong_snapshot = replace(snapshot, corpus_sha256=SHA[15])
    wrong_analysis = make_normalized_analysis(wrong_snapshot, make_analysis_scope(wrong_snapshot))
    with pytest.raises(ValueError, match="source manifest"):
        verify_native_analysis(snapshot, wrong_analysis)
    compiler_run = replace(
        analysis.run, analysis_mode="precise", evidence_level=EvidenceLevel.COMPILER,
        receipt_sha256=SHA[11], receipt_output_sha256=SHA[12], consent_grant_id="grant:test",
        consent_revision=1, lease_id="lease:test",
    )
    compiler_receipt = AnalyzerReceipt(
        receipt_sha256=SHA[11],
        output_sha256=SHA[12],
        source_manifest_sha256=compiler_run.identity.source_manifest_sha256,
        analysis_sha256=compiler_run.identity.analysis_sha256,
    )
    with pytest.raises(ValueError, match="native-syntax"):
        verify_native_analysis(snapshot, replace(analysis, run=compiler_run, receipt=compiler_receipt))
    with pytest.raises(ValueError, match="syntax evidence"):
        verify_native_analysis(snapshot, replace(analysis, symbols=(replace(analysis.symbols[0], evidence_level=EvidenceLevel.LEXICAL),)))
