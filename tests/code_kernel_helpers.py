"""Shared fixtures and helpers for code-kernel tests."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from corpus_snapshot import (
    CapturedSource,
    CorpusSnapshot,
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


_REGULAR_TAR_TYPES = {tarfile.REGTYPE, tarfile.AREGTYPE}


def _default_pyright_entries(
    package_bytes: bytes | None,
    server_bytes: bytes,
    include_package: bool,
    include_server: bool,
) -> tuple[PyrightTarEntry, ...]:
    """The three members a real npm tarball carries, minus what a test drops."""
    manifest = package_bytes or canonical_json_bytes(
        {"name": "pyright", "version": "1.1.411"}
    )
    optional = (
        (include_package, PyrightTarEntry("package/package.json", manifest)),
        (include_server, PyrightTarEntry("package/langserver.index.js", server_bytes)),
    )
    return (
        PyrightTarEntry("package", kind=tarfile.DIRTYPE),
        *(entry for wanted, entry in optional if wanted),
    )


def _pyright_tar_info(entry: PyrightTarEntry) -> tarfile.TarInfo:
    info = tarfile.TarInfo(entry.name)
    info.type = entry.kind
    info.linkname = entry.linkname
    info.mode = 0o777
    info.uid = 123
    info.gid = 456
    info.mtime = 789
    info.pax_headers = dict(entry.pax_headers or {})
    info.size = len(entry.data) if entry.kind in _REGULAR_TAR_TYPES else 0
    return info


def _write_pyright_tarball(
    destination: Path, entries: tuple[PyrightTarEntry, ...], tar_format: int
) -> None:
    with destination.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tar_format) as archive:
                for entry in entries:
                    info = _pyright_tar_info(entry)
                    payload = io.BytesIO(entry.data) if info.size else None
                    archive.addfile(info, payload)


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
        entries = _default_pyright_entries(
            package_bytes, server_bytes, include_package, include_server
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_pyright_tarball(destination, entries, tar_format)
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


def _lockfile_entry(
    integrity: str, url: str, package_version: str, link: bool
) -> dict[str, object]:
    entry: dict[str, object] = {
        "integrity": integrity,
        "resolved": url,
        "version": package_version,
    }
    entry.update({"link": True} if link else {})
    return entry


def _lockfile_document(version: int, entry: dict[str, object]) -> dict[str, object]:
    """npm's two shapes: v1 keys by name, everything later by install path."""
    if version == 1:
        return {"dependencies": {"pyright": entry}, "lockfileVersion": 1}
    return {"lockfileVersion": version, "packages": {"node_modules/pyright": entry}}


def _write_pyright_lockfile(
    destination: Path, version: int | None, entry: dict[str, object]
) -> None:
    if version is None:
        return
    (destination / "package-lock.json").write_text(
        json.dumps(_lockfile_document(version, entry), sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


_DEFAULT_BUNDLES = {"pyright-langserver.js": b"synthetic pyright bundle\n"}


def _write_pyright_bundles(package_root: Path, bundles: Mapping[str, bytes]) -> None:
    """The `package/dist` files the shim loads, which the receipt now attests."""
    from pyright_profile import PYRIGHT_EXECUTED_TREE_RELATIVE

    dist = package_root.parent / PYRIGHT_EXECUTED_TREE_RELATIVE
    dist.mkdir(parents=True, exist_ok=True)
    for name, content in bundles.items():
        (dist / name).write_bytes(content)


def pyright_executed_tree_sha256(
    server_bytes: bytes, bundles: Mapping[str, bytes]
) -> str:
    """The receipt's digest over the shim and the `package/dist` files with it."""
    from pyright_profile import (
        PYRIGHT_EXECUTED_TREE_RELATIVE,
        PYRIGHT_SERVER_RELATIVE,
        executed_tree_digest,
    )

    entries = [
        (PYRIGHT_SERVER_RELATIVE.as_posix(), hashlib.sha256(server_bytes).hexdigest())
    ]
    entries.extend(
        (
            (PYRIGHT_EXECUTED_TREE_RELATIVE / name).as_posix(),
            hashlib.sha256(content).hexdigest(),
        )
        for name, content in bundles.items()
    )
    return executed_tree_digest(entries)


def _write_pyright_manifest(
    destination: Path,
    build_manifest,
    server_bytes: bytes,
    bundles: Mapping[str, bytes],
    overrides: Mapping[str, object] | None,
) -> None:
    manifest = build_manifest(
        server_sha256=hashlib.sha256(server_bytes).hexdigest(),
        executed_tree_sha256=pyright_executed_tree_sha256(server_bytes, bundles),
    )
    manifest.update(dict(overrides or {}))
    (destination / "install-manifest.json").write_bytes(canonical_json_bytes(manifest))


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
    bundle_bytes: Mapping[str, bytes] | None = None,
    manifest_overrides: Mapping[str, object] | None = None,
) -> Path:
    """Create a synthetic package tree without invoking npm or the network."""
    from pyright_profile import PYRIGHT_PACKAGE_INTEGRITY, PYRIGHT_PACKAGE_URL

    integrity = PYRIGHT_PACKAGE_INTEGRITY if integrity is None else integrity
    package_root = destination / "package" if managed else destination / "node_modules/pyright"
    package_root.mkdir(parents=True, exist_ok=True)
    server = package_root / "langserver.index.js"
    server.write_bytes(server_bytes)
    (package_root / "package.json").write_bytes(
        canonical_json_bytes({"name": package_name, "version": package_version})
    )

    if not managed:
        _write_pyright_lockfile(
            destination,
            lockfile_version,
            _lockfile_entry(integrity, PYRIGHT_PACKAGE_URL, package_version, lockfile_link),
        )
    if managed:
        _write_managed_receipt(
            destination, package_root, server_bytes, bundle_bytes, manifest_overrides
        )
    return server


def _write_managed_receipt(
    destination: Path,
    package_root: Path,
    server_bytes: bytes,
    bundle_bytes: Mapping[str, bytes] | None,
    manifest_overrides: Mapping[str, object] | None,
) -> None:
    from pyright_profile import build_pyright_install_manifest

    bundles = _DEFAULT_BUNDLES if bundle_bytes is None else dict(bundle_bytes)
    _write_pyright_bundles(package_root, bundles)
    _write_pyright_manifest(
        destination,
        build_pyright_install_manifest,
        server_bytes,
        bundles,
        manifest_overrides,
    )


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


def build_fixture_generation(tmp_path: Path, *, generation_id: str, graph_schema=None):
    """One fixture generation of the published schema, built through the builder."""
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog

    records = basic_graph_records()
    options = {} if graph_schema is None else {"graph_schema": graph_schema}
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
