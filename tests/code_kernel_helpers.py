"""Shared fixtures and helpers for code-kernel tests."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
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
    Diagnostic,
    DiagnosticSeverity,
    EvidenceLevel,
    ExpectedSource,
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
)
from corpus_snapshot import (
    CapturedSource,
    CodeCaptureContract,
    CodeCaptureFile,
    CorpusSnapshot,
    DirectoryMembership,
    FileStatMetadata,
    RepositoryCodeLimits,
    RepositoryCodePolicy,
    SnapshotPolicy,
    SourceMetadata,
    SourceRecord,
    canonical_retrieval_chunks,
    canonical_source_manifest_sha256,
)
from reliable_memory import canonical_json_bytes, validate_state_root
from repository_scope import sanitized_git_environment

FIXTURE_ROOT = Path(__file__).parent / "fixtures/code_kernel/python"


@dataclass(frozen=True, slots=True)
class PyrightTarEntry:
    name: str
    data: bytes = b""
    kind: bytes = tarfile.REGTYPE
    linkname: str = ""
    pax_headers: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class PyrightInstallArtifactFixture:
    path: Path
    package_sha256: str
    package_integrity: str
    server_bytes: bytes


@dataclass(frozen=True, slots=True)
class SemanticPyrightFixture:
    identity: object
    config_path: Path
    event_log: Path

    def events(self) -> tuple[dict[str, object], ...]:
        if not self.event_log.exists():
            return ()
        return tuple(
            json.loads(line)
            for line in self.event_log.read_text(encoding="utf-8").splitlines()
            if line
        )


def create_semantic_pyright_fixture(
    repository: Path,
    *,
    config: Mapping[str, object] | None = None,
) -> SemanticPyrightFixture:
    """Create a qualified identity backed by the deterministic fake LSP peer."""
    from pyright_profile import (
        PYRIGHT_CONFIGURATION_SHA256,
        PYRIGHT_INITIALIZATION_OPTIONS_SHA256,
        PyrightIdentity,
    )

    server = Path(__file__).with_name("fake_lsp_server.py").resolve()
    node = Path(sys.executable).resolve()
    event_log = repository / ".git" / "fake-lsp-events.jsonl"
    config_path = repository / ".fake-lsp-server.json"
    value = dict(config or {})
    value["event_log"] = str(event_log)
    config_path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )
    identity = PyrightIdentity(
        status="qualified",
        source="test-fixture",
        version="1.1.411",
        node_executable=node,
        node_version="v22.0.0",
        node_major=22,
        server_executable=server,
        executable_sha256=hashlib.sha256(server.read_bytes()).hexdigest(),
        package_sha256=hashlib.sha256(b"semantic-pyright-fixture").hexdigest(),
        initialization_options_sha256=PYRIGHT_INITIALIZATION_OPTIONS_SHA256,
        configuration_sha256=PYRIGHT_CONFIGURATION_SHA256,
        qualified=True,
        degradation_codes=(),
    )
    return SemanticPyrightFixture(identity, config_path, event_log)


def create_pyright_install_artifact(
    destination: Path,
    *,
    entries: tuple[PyrightTarEntry, ...] | None = None,
    package_bytes: bytes | None = None,
    server_bytes: bytes = b"synthetic pyright language server\n",
    include_package: bool = True,
    include_server: bool = True,
    tar_format: int = tarfile.PAX_FORMAT,
) -> PyrightInstallArtifactFixture:
    """Create a deterministic synthetic npm-style Pyright tarball."""
    if entries is None:
        package_bytes = package_bytes or canonical_json_bytes(
            {"name": "pyright", "version": "1.1.411"}
        )
        values = [PyrightTarEntry("package", kind=tarfile.DIRTYPE)]
        if include_package:
            values.append(PyrightTarEntry("package/package.json", package_bytes))
        if include_server:
            values.append(PyrightTarEntry("package/langserver.index.js", server_bytes))
        entries = tuple(values)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tar_format) as archive:
                for entry in entries:
                    info = tarfile.TarInfo(entry.name)
                    info.type = entry.kind
                    info.linkname = entry.linkname
                    info.mode = 0o777
                    info.uid = 123
                    info.gid = 456
                    info.mtime = 789
                    info.pax_headers = dict(entry.pax_headers or {})
                    regular = entry.kind in {tarfile.REGTYPE, tarfile.AREGTYPE}
                    info.size = len(entry.data) if regular else 0
                    archive.addfile(info, io.BytesIO(entry.data) if regular else None)

    content = destination.read_bytes()
    return PyrightInstallArtifactFixture(
        path=destination,
        package_sha256=hashlib.sha256(content).hexdigest(),
        package_integrity="sha512-"
        + base64.b64encode(hashlib.sha512(content).digest()).decode("ascii"),
        server_bytes=server_bytes,
    )


def use_pyright_install_artifact_identity(
    monkeypatch: pytest.MonkeyPatch,
    artifact: PyrightInstallArtifactFixture,
) -> None:
    """Point the approved profile contract at one synthetic test artifact."""
    import pyright_profile

    monkeypatch.setattr(pyright_profile, "PYRIGHT_PACKAGE_SHA256", artifact.package_sha256)
    monkeypatch.setattr(pyright_profile, "PYRIGHT_PACKAGE_INTEGRITY", artifact.package_integrity)


def copy_python_fixture(destination: Path) -> Path:
    shutil.copytree(FIXTURE_ROOT, destination)
    return destination


def create_python_repository(destination: Path) -> Path:
    copy_python_fixture(destination)
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
            # A guard against a hung git, not a performance budget: `git add .`
            # over a fixture tree took longer than ten seconds on a loaded
            # hosted Windows runner and failed the tests that used it.
            timeout=120,
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


def create_pyright_fixture(
    destination: Path,
    *,
    managed: bool = False,
    package_name: str = "pyright",
    package_version: str = "1.1.411",
    integrity: str | None = None,
    lockfile_version: int | None = 3,
    lockfile_link: bool = False,
    server_bytes: bytes = b"synthetic pyright language server\n",
    manifest_overrides: Mapping[str, object] | None = None,
) -> Path:
    """Create a synthetic package tree without invoking npm or the network."""
    from pyright_profile import (
        PYRIGHT_PACKAGE_INTEGRITY,
        PYRIGHT_PACKAGE_URL,
        build_pyright_install_manifest,
    )

    integrity = PYRIGHT_PACKAGE_INTEGRITY if integrity is None else integrity
    package_root = destination / "package" if managed else destination / "node_modules/pyright"
    package_root.mkdir(parents=True, exist_ok=True)
    server = package_root / "langserver.index.js"
    server.write_bytes(server_bytes)
    (package_root / "package.json").write_bytes(
        canonical_json_bytes({"name": package_name, "version": package_version})
    )

    if not managed and lockfile_version is not None:
        entry: dict[str, object] = {
            "integrity": integrity,
            "resolved": PYRIGHT_PACKAGE_URL,
            "version": package_version,
        }
        if lockfile_link:
            entry["link"] = True
        if lockfile_version == 1:
            lockfile: dict[str, object] = {
                "dependencies": {"pyright": entry},
                "lockfileVersion": 1,
            }
        else:
            lockfile = {
                "lockfileVersion": lockfile_version,
                "packages": {"node_modules/pyright": entry},
            }
        (destination / "package-lock.json").write_text(
            json.dumps(lockfile, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    if managed:
        manifest = build_pyright_install_manifest(
            server_sha256=hashlib.sha256(server_bytes).hexdigest()
        )
        if manifest_overrides is not None:
            manifest.update(manifest_overrides)
        (destination / "install-manifest.json").write_bytes(canonical_json_bytes(manifest))
    return server


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


def make_run(
    snapshot: CorpusSnapshot, outcome: str = "complete", repository_scope=None
) -> AnalysisRun:
    scope = make_analysis_scope(snapshot)
    identity = make_analysis_identity(snapshot, scope)
    return AnalysisRun(
        run_id=scope.run_id,
        identity=identity,
        source_manifest_sha256=snapshot.corpus_sha256,
        analysis_mode="native-syntax",
        repository_id=(
            repository_scope.repository_id
            if repository_scope is not None
            else "repository:test-fixture"
        ),
        checkout_id=(
            repository_scope.checkout_id
            if repository_scope is not None
            else "checkout:test-fixture"
        ),
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
    repository_scope=None,
) -> NormalizedAnalysis:
    run = make_run(snapshot, repository_scope=repository_scope)
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
                "source_id": "source:app.py",
                "relative_path": "app.py",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
                "media_type": "text/x-python",
                "language": "python",
                "git_oid": None,
            }
        ],
        "source_bytes": {"source:app.py": content},
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
                "source_id": "source:app.py",
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
                "source_id": "source:app.py",
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
        chunks=tuple(
            chunk
            for source in captured
            for chunk in canonical_retrieval_chunks(
                source_id=source.record.logical_id,
                source_path=source.record.relative_path,
                source_sha256=source.record.sha256,
                content=source.content,
            )
        ),
        corpus_sha256=canonical_source_manifest_sha256(
            (source.record for source in captured), policy
        ),
        policy=policy,
    )


def captured_snapshot_for_records(records: dict[str, object]) -> CorpusSnapshot:
    """Add the canonical capture fixture required for analyzer-backed generations."""
    snapshot = snapshot_for_records(records)
    files = tuple(
        CodeCaptureFile(
            source.record.logical_id,
            source.record.relative_path,
            source.record.sha256,
            FileStatMetadata(source.record.size, 0, 0, stat.S_IFREG, 0, 0),
        )
        for source in snapshot.sources
    )
    empty_entries = hashlib.sha256(canonical_json_bytes([])).hexdigest()
    directories = (
        DirectoryMembership("fixture-a", 0, empty_entries),
        DirectoryMembership("fixture-b", 0, empty_entries),
    )
    membership = hashlib.sha256(
        canonical_json_bytes(
            {
                "files": [
                    {
                        "source_id": item.source_id,
                        "relative_path": item.relative_path,
                        "sha256": item.sha256,
                        "stat": {
                            name: getattr(item.stat, name)
                            for name in item.stat.__dataclass_fields__
                        },
                    }
                    for item in files
                ],
                "directories": [
                {
                    "relative_path": item.relative_path,
                    "entry_count": item.entry_count,
                    "entries_sha256": item.entries_sha256,
                }
                for item in directories
                ],
            }
        )
    ).hexdigest()
    contract = CodeCaptureContract(
        policy=RepositoryCodePolicy(
            roots=tuple(
                sorted(
                    {
                        *(item.relative_path for item in files),
                        *(item.relative_path for item in directories),
                    }
                )
            ),
            include_globs=("**",),
            ignore_globs=(),
            suffixes=tuple(sorted({Path(item.relative_path).suffix.casefold() for item in files})),
        ),
        limits=RepositoryCodeLimits(),
        files=files,
        directories=directories,
        membership_sha256=membership,
    )
    return replace(snapshot, code_capture=contract)


def make_normalized_analysis_for_records(
    records: dict[str, object], repository_scope=None
) -> NormalizedAnalysis:
    snapshot = snapshot_for_records(records)
    scope = make_analysis_scope(snapshot)
    run = replace(
        make_run(snapshot),
        declared_capabilities=(
            Capability.CALLS,
            Capability.DEFINITIONS,
            Capability.DIAGNOSTICS,
        ),
        **(
            {
                "repository_id": repository_scope.repository_id,
                "checkout_id": repository_scope.checkout_id,
            }
            if repository_scope is not None
            else {}
        ),
    )
    source = scope.expected_sources[0]
    symbol_id = "claim:symbol"
    relationship_id = "claim:relationship"
    diagnostic_id = "diagnostic:fixture"
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
        diagnostics=(
            Diagnostic(
                diagnostic_id=diagnostic_id,
                run_id=run.run_id,
                scope_id=scope.scope_id,
                source_id=source.source_id,
                capability=Capability.DIAGNOSTICS,
                severity=DiagnosticSeverity.WARNING,
                code="fixture",
                message="fixture diagnostic",
                range=PositionRange(0, 3),
                evidence_level=EvidenceLevel.SYNTAX,
                related=(
                    RelatedLocation(
                        source_id=source.source_id,
                        range=PositionRange(4, 10),
                        message="related fixture",
                    ),
                ),
            ),
        ),
        validity=(
            Validity(
                validity_id="validity:diagnostic",
                subject_kind=SubjectKind.DIAGNOSTIC,
                subject_id=diagnostic_id,
                status=ValidityStatus.CURRENT,
                stale_reason=None,
            ),
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


def make_unminted_verified_subclass(records: dict[str, object]) -> VerifiedAnalysisBatch:
    from code_intelligence import verify_native_analysis

    verified = verify_native_analysis(
        snapshot_for_records(records), make_normalized_analysis_for_records(records)
    )

    class UnmintedVerifiedAnalysisBatch(VerifiedAnalysisBatch):
        pass

    forged = object.__new__(UnmintedVerifiedAnalysisBatch)
    for name in VerifiedAnalysisBatch.__dataclass_fields__:
        object.__setattr__(forged, name, getattr(verified, name))
    assert isinstance(forged, VerifiedAnalysisBatch)
    assert type(forged) is not VerifiedAnalysisBatch
    return forged


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
        from repository_scope import resolve_repository_scope

        repository = tmp_path / "repository"
        repository.mkdir(parents=True, exist_ok=True)
        repository_scope = resolve_repository_scope(repository)
        snapshot = captured_snapshot_for_records(records)
        options["verified_analyses"] = (
            verify_native_analysis(
                snapshot,
                make_normalized_analysis_for_records(records, repository_scope),
            ),
        )
        options.update(
            snapshot=snapshot,
            repository_scope=repository_scope,
            code_capture=snapshot.code_capture,
        )
    return build_full_generation(
        GenerationCatalog(tmp_path / "state"),
        generation_id=generation_id,
        activate=False,
        **options,
        **records,
    )


def publish_v2_fixture(tmp_path: Path, generation_id: str = "v2"):
    return _build_non_code_v2_generation(tmp_path, generation_id=generation_id)


def _build_non_code_v2_generation(
    tmp_path: Path, generation_id: str = "v2", *, activate: bool = False
):
    """Publish ordinary graph-v2 records without a repository capture contract."""
    from evidence_graph import GraphSchema

    records = basic_graph_records()
    result = __import__("evidence_graph_builder").build_full_generation(
        __import__("generation_catalog").GenerationCatalog(tmp_path / "state"),
        generation_id=generation_id,
        graph_schema=GraphSchema.V2,
        code_capture=None,
        activate=activate,
        **records,
    )
    assert "code_capture" not in result.manifest
    return result


@pytest.fixture
def non_code_v2_generation(tmp_path: Path):
    result = _build_non_code_v2_generation(tmp_path, activate=True)
    assert result.activated is True
    return result


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
