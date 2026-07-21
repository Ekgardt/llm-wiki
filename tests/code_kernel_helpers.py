"""Shared fixtures and helpers for code-kernel tests."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
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
    NormalizedAnalysis,
    PositionEncoding,
    PositionRange,
    SubjectKind,
    SymbolClaim,
    SymbolIdentity,
    SymbolRole,
    Validity,
    ValidityStatus,
)
from corpus_snapshot import CapturedSource, CorpusSnapshot
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
    source_ids = tuple(sorted(source.record.logical_id for source in snapshot.sources))
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
        expected_source_ids=source_ids,
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
                    validity_id="validity:" + hashlib.sha256(claim_id.encode("utf-8")).hexdigest(),
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


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    validate_state_root(root)
    return root


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return create_python_repository(tmp_path / "repository")
