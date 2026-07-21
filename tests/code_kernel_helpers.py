"""Shared fixtures and helpers for code-kernel tests."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from code_intelligence import (
    AnalysisIdentity,
    AnalysisOutcome,
    AnalysisRun,
    AnalysisScope,
    Capability,
    Coverage,
    CoverageStatus,
    EvidenceLevel,
    ExpectedSource,
    NormalizedAnalysis,
    PositionEncoding,
    PositionRange,
    Relationship,
    RelationshipClaim,
    RelationshipResolution,
    SubjectKind,
    SymbolClaim,
    SymbolIdentity,
    SymbolRole,
    Validity,
    ValidityStatus,
)
from corpus_snapshot import (
    CapturedSource,
    CorpusSnapshot,
    SnapshotPolicy,
    SourceMetadata,
    SourceRecord,
    canonical_source_manifest_sha256,
)
from reliable_memory import canonical_json_bytes, validate_state_root
from repository_scope import sanitized_git_environment

FIXTURE_ROOT = Path(__file__).parent / "fixtures/code_kernel/python"


def create_python_repository(destination: Path) -> Path:
    shutil.copytree(FIXTURE_ROOT, destination)
    environment = sanitized_git_environment()
    for name in tuple(environment):
        if name in {"GIT_DEFAULT_HASH", "GIT_TEMPLATE_DIR"} or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            environment.pop(name)
    isolation = destination / ".git-test-isolation"
    hooks = isolation / "hooks"
    template = isolation / "template"
    hooks.mkdir(parents=True)
    template.mkdir(parents=True)
    environment.update(
        GIT_AUTHOR_DATE="@946684800 +0000",
        GIT_AUTHOR_EMAIL="fixture@example.test",
        GIT_AUTHOR_NAME="Code Kernel Fixture",
        GIT_COMMITTER_DATE="@946684800 +0000",
        GIT_COMMITTER_EMAIL="fixture@example.test",
        GIT_COMMITTER_NAME="Code Kernel Fixture",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_TEMPLATE_DIR=str(template.resolve()),
        GIT_TERMINAL_PROMPT="0",
    )

    def run_git(*arguments: str) -> None:
        subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={hooks.resolve()}",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "tag.gpgSign=false",
                *arguments,
            ],
            cwd=destination,
            env=environment,
            check=True,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )

    run_git("init", "--initial-branch=main", f"--template={template.resolve()}")
    run_git("config", "user.email", "fixture@example.test")
    run_git("config", "user.name", "Code Kernel Fixture")
    run_git("add", ".")
    run_git("commit", "-m", "fixture")
    return destination


def fixture_digest(root: Path) -> str:
    values = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }
    return hashlib.sha256(canonical_json_bytes(values)).hexdigest()


def source_bytes(snapshot: CorpusSnapshot, source_id: str) -> bytes:
    matches = [
        source.content
        for source in snapshot.sources
        if source.record.logical_id == source_id
    ]
    if len(matches) != 1:
        raise KeyError(source_id)
    return matches[0]


def source_by_path(snapshot: CorpusSnapshot, relative_path: str) -> CapturedSource:
    matches = [
        source
        for source in snapshot.sources
        if source.record.relative_path == relative_path
    ]
    if len(matches) != 1:
        raise KeyError(relative_path)
    return matches[0]


def make_analysis_scope(snapshot: CorpusSnapshot) -> AnalysisScope:
    expected_sources = tuple(
        sorted(
            (
                ExpectedSource(source.record.logical_id, source.record.sha256, "included")
                for source in snapshot.sources
            ),
            key=lambda item: item.source_id,
        )
    )
    source_ids = tuple(item.source_id for item in expected_sources)
    run_id = "run:" + hashlib.sha256(
        canonical_json_bytes(
            {"kind": "native-test-run", "source_manifest_sha256": snapshot.corpus_sha256}
        )
    ).hexdigest()
    scope_id = "scope:" + hashlib.sha256(
        canonical_json_bytes(
            {
                "build_configuration": "default",
                "build_target": "default",
                "run_id": run_id,
                "source_ids": list(source_ids),
            }
        )
    ).hexdigest()
    return AnalysisScope(
        scope_id=scope_id,
        run_id=run_id,
        source_manifest_sha256=snapshot.corpus_sha256,
        build_target="default",
        build_configuration="default",
        expected_sources=expected_sources,
        generated_sources="not-required",
        dependency_resolution="complete",
        analyzer_support="complete",
    )


def make_analysis_identity(
    snapshot: CorpusSnapshot,
    scope: AnalysisScope,
) -> AnalysisIdentity:
    if scope.source_manifest_sha256 != snapshot.corpus_sha256:
        raise ValueError("scope source manifest must match snapshot")
    components = {
        name: hashlib.sha256(
            canonical_json_bytes(
                {
                    "component": name,
                    "scope_id": scope.scope_id,
                    "source_manifest_sha256": snapshot.corpus_sha256,
                }
            )
        ).hexdigest()
        for name in (
            "manifest_sha256",
            "lockfile_sha256",
            "sdk_sha256",
            "target_sha256",
            "configuration_sha256",
            "feature_sha256",
            "invocation_sha256",
            "environment_sha256",
            "dependency_state_sha256",
        )
    }
    return AnalysisIdentity.create(
        source_manifest_sha256=snapshot.corpus_sha256,
        position_encoding=PositionEncoding.UTF8,
        **components,
    )


def make_run(snapshot: CorpusSnapshot, outcome: str = "complete") -> AnalysisRun:
    scope = make_analysis_scope(snapshot)
    identity = make_analysis_identity(snapshot, scope)
    return AnalysisRun(
        run_id=scope.run_id,
        identity=identity,
        source_manifest_sha256=snapshot.corpus_sha256,
        analysis_mode="native-syntax",
        repository_id="repository:test-fixture",
        checkout_id="checkout:test-fixture",
        source_generation_id="generation:test-fixture",
        analyzer_family="native-test",
        analyzer_version="1",
        protocol="native",
        protocol_version="1",
        executable_sha256=hashlib.sha256(b"native-test").hexdigest(),
        declared_capabilities=(Capability.DEFINITIONS,),
        evidence_level=EvidenceLevel.SYNTAX,
        qualified=True,
        outcome=AnalysisOutcome(outcome),
        receipt_sha256=None,
        receipt_output_sha256=None,
        consent_grant_id=None,
        consent_revision=None,
        lease_id=None,
        started_at="2026-07-21T00:00:00Z",
        ended_at="2026-07-21T00:00:01Z",
    )


def make_normalized_analysis(
    snapshot: CorpusSnapshot,
    scope: AnalysisScope,
) -> NormalizedAnalysis:
    run = make_run(snapshot)
    if scope.run_id != run.run_id:
        raise ValueError("scope run_id must match fixture run")
    coverage = tuple(
        Coverage(
            scope_id=scope.scope_id,
            source_id=source_id,
            capability=Capability.DEFINITIONS,
            status=CoverageStatus.COMPLETE,
            closed_world_eligible=True,
            reason=None,
        )
        for source_id in scope.expected_source_ids
    )
    symbols: tuple[SymbolClaim, ...] = ()
    validity: tuple[Validity, ...] = ()
    if scope.expected_source_ids:
        source_id = scope.expected_source_ids[0]
        content = source_bytes(snapshot, source_id)
        if content:
            claim_id = "claim:" + hashlib.sha256(
                canonical_json_bytes(
                    {"scope_id": scope.scope_id, "source_id": source_id, "kind": "fixture"}
                )
            ).hexdigest()
            symbols = (
                SymbolClaim(
                    claim_id=claim_id,
                    run_id=run.run_id,
                    scope_id=scope.scope_id,
                    source_id=source_id,
                    capability=Capability.DEFINITIONS,
                    identity=SymbolIdentity("fixture", source_id),
                    display_name="fixture",
                    symbol_kind="fixture",
                    role=SymbolRole.DEFINITION,
                    range=PositionRange(0, 1),
                    evidence_level=EvidenceLevel.SYNTAX,
                    ambiguity=False,
                ),
            )
            validity = (
                Validity(
                    validity_id="validity:"
                    + hashlib.sha256(claim_id.encode("utf-8")).hexdigest(),
                    subject_kind=SubjectKind.SYMBOL,
                    subject_id=claim_id,
                    status=ValidityStatus.CURRENT,
                    stale_reason=None,
                ),
            )
    return NormalizedAnalysis(
        run=run,
        scopes=(scope,),
        coverage=coverage,
        symbols=symbols,
        relationships=(),
        diagnostics=(),
        validity=validity,
        receipt=None,
    )


def basic_graph_records() -> dict[str, object]:
    content = b"def caller():\n    callee()\n"
    return {
        "sources": [
            {
                "source_id": "source",
                "relative_path": "app.py",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
                "media_type": "text/x-python",
                "language": "python",
                "git_oid": None,
            }
        ],
        "source_bytes": {"source": content},
        "nodes": [
            {
                "node_id": "caller",
                "kind": "function",
                "identity_scheme": "python/v1",
                "identity_key": "app:caller",
                "metadata": {"name": "caller"},
            },
            {
                "node_id": "callee",
                "kind": "function",
                "identity_scheme": "python/v1",
                "identity_key": "app:callee",
                "metadata": {"name": "callee"},
            },
        ],
        "occurrences": [
            {
                "occurrence_id": "occurrence",
                "node_id": "caller",
                "source_id": "source",
                "role": "definition",
                "byte_start": 0,
                "byte_end": 12,
                "line_start": 1,
                "line_end": 1,
            }
        ],
        "assertions": [
            {
                "assertion_id": "assertion",
                "source_node_id": "caller",
                "edge_type": "CALLS",
                "target_node_id": "callee",
                "literal": None,
                "confidence": "high",
                "authority": "ai-derived",
                "resolution": "resolved",
                "extractor": "python/v1",
            }
        ],
        "evidence": [
            {
                "evidence_id": "evidence",
                "assertion_id": "assertion",
                "observation_id": None,
                "source_id": "source",
                "byte_start": 18,
                "byte_end": 26,
                "span_sha256": hashlib.sha256(content[18:26]).hexdigest(),
            }
        ],
        "observations": [],
        "dependencies": [],
    }


def snapshot_for_records(records: dict[str, object]) -> CorpusSnapshot:
    policy = SnapshotPolicy(
        daily_paths=(),
        code_roots=(),
        include_historical=False,
        as_of=None,
        max_files=10_000,
        max_file_bytes=16 * 1024 * 1024,
        max_total_bytes=512 * 1024 * 1024,
        max_entries=50_000,
        max_directories=5_000,
        max_depth=32,
    )
    source_bytes_by_id = records["source_bytes"]
    captured = tuple(
        CapturedSource(
            record=SourceRecord(
                logical_id=source["source_id"],
                relative_path=source["relative_path"],
                sha256=source["sha256"],
                size=source["size"],
                media_type=source["media_type"],
                language=source["language"],
                git_oid=source["git_oid"],
            ),
            metadata=SourceMetadata(type="code", language=source["language"]),
            content=source_bytes_by_id[source["source_id"]],
        )
        for source in records["sources"]
    )
    return CorpusSnapshot(
        sources=captured,
        chunks=(),
        corpus_sha256=canonical_source_manifest_sha256(
            (source.record for source in captured), policy
        ),
        policy=policy,
    )


def make_normalized_analysis_for_records(records: dict[str, object]) -> NormalizedAnalysis:
    snapshot = snapshot_for_records(records)
    scope = make_analysis_scope(snapshot)
    run = replace(
        make_run(snapshot),
        declared_capabilities=(Capability.CALLS, Capability.DEFINITIONS),
    )
    source = scope.expected_sources[0]
    symbol_id = "claim:symbol"
    relationship_id = "claim:relationship"
    symbol_identity = SymbolIdentity("python/v1", "app:caller")
    return NormalizedAnalysis(
        run=run,
        scopes=(scope,),
        coverage=tuple(
            Coverage(
                scope_id=scope.scope_id,
                source_id=expected.source_id,
                capability=capability,
                status=CoverageStatus.COMPLETE,
                closed_world_eligible=True,
                reason=None,
            )
            for expected in scope.expected_sources
            for capability in run.declared_capabilities
        ),
        symbols=(
            SymbolClaim(
                claim_id=symbol_id,
                run_id=run.run_id,
                scope_id=scope.scope_id,
                source_id=source.source_id,
                capability=Capability.DEFINITIONS,
                identity=symbol_identity,
                display_name="caller",
                symbol_kind="function",
                role=SymbolRole.DEFINITION,
                range=PositionRange(4, 10),
                evidence_level=EvidenceLevel.SYNTAX,
                ambiguity=False,
            ),
        ),
        relationships=(
            RelationshipClaim(
                claim_id=relationship_id,
                run_id=run.run_id,
                scope_id=scope.scope_id,
                source_id=source.source_id,
                source_identity=symbol_identity,
                relation=Relationship.CALLS,
                capability=Capability.CALLS,
                target_identity=None,
                target_text="callee",
                resolution=RelationshipResolution.UNRESOLVED,
                range=PositionRange(18, 26),
                evidence_level=EvidenceLevel.SYNTAX,
                ambiguity=False,
            ),
        ),
        diagnostics=(),
        validity=(
            Validity(
                validity_id="validity:relationship",
                subject_kind=SubjectKind.RELATIONSHIP,
                subject_id=relationship_id,
                status=ValidityStatus.CURRENT,
                stale_reason=None,
            ),
            Validity(
                validity_id="validity:symbol",
                subject_kind=SubjectKind.SYMBOL,
                subject_id=symbol_id,
                status=ValidityStatus.CURRENT,
                stale_reason=None,
            ),
        ),
        receipt=None,
    )


def build_fixture_generation(
    tmp_path: Path,
    *,
    generation_id: str,
    graph_schema=None,
):
    from code_intelligence import verify_native_analysis
    from evidence_graph import GraphSchema
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog

    records = basic_graph_records()
    options = {}
    if graph_schema is not None:
        options["graph_schema"] = graph_schema
    if graph_schema is GraphSchema.V3:
        options["verified_analyses"] = (
            verify_native_analysis(
                snapshot_for_records(records), make_normalized_analysis_for_records(records)
            ),
        )
    return build_full_generation(
        GenerationCatalog(tmp_path / "state"),
        generation_id=generation_id,
        activate=False,
        **options,
        **records,
    )


def publish_v2_fixture(tmp_path: Path, generation_id: str = "v2"):
    from evidence_graph import GraphSchema

    return build_fixture_generation(
        tmp_path, generation_id=generation_id, graph_schema=GraphSchema.V2
    )


def publish_v3_fixture(tmp_path: Path, generation_id: str = "v3"):
    from evidence_graph import GraphSchema

    return build_fixture_generation(
        tmp_path, generation_id=generation_id, graph_schema=GraphSchema.V3
    )


def open_v3_fixture(tmp_path: Path):
    from evidence_graph import EvidenceGraph, GraphSchema

    result = publish_v3_fixture(tmp_path)
    return EvidenceGraph(
        result.generation_path / "evidence.sqlite3",
        state_root=tmp_path / "state",
        schema=GraphSchema.V3,
    )


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    validate_state_root(root)
    return root


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return create_python_repository(tmp_path / "repository")


@pytest.fixture
def catalog(state_root: Path):
    from generation_catalog import GenerationCatalog

    return GenerationCatalog(state_root)
