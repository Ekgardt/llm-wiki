"""Shared fixtures and helpers for code-kernel tests."""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from corpus_snapshot import (
    CapturedSource,
    CorpusSnapshot,
)
from reliable_memory import canonical_json_bytes

from tests.shared_fixtures import FIXTURE_ROOT as FIXTURE_ROOT
from tests.shared_fixtures import catalog as catalog
from tests.shared_fixtures import copy_python_fixture as copy_python_fixture
from tests.shared_fixtures import create_python_repository as create_python_repository
from tests.shared_fixtures import repository as repository
from tests.shared_fixtures import state_root as state_root


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


def package_fake_lsp_server(destination: Path) -> Path:
    """Keep every fixture dependency inside the verified launch artifact."""
    server = Path(__file__).with_name("fake_lsp_server.py")
    with zipfile.ZipFile(destination, "w") as archive:
        archive.write(server, "__main__.py")
        archive.write(server.with_name("fake_process_tree.py"), "fake_process_tree.py")
    return destination


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

    server = package_fake_lsp_server(repository / ".git" / "fake-lsp-server.pyz")
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
    from llm_wiki import pyright_configuration

    package_root = _pyright_package_root(destination, managed)
    server = _write_pyright_package(package_root, package_name, package_version, server_bytes)
    if managed:
        _write_managed_receipt(
            destination, package_root, server_bytes, bundle_bytes, manifest_overrides
        )
        return server
    _write_pyright_lockfile(
        destination,
        lockfile_version,
        _lockfile_entry(
            _requested_integrity(integrity),
            pyright_configuration.PYRIGHT_PACKAGE_URL,
            package_version,
            lockfile_link,
        ),
    )
    return server


def _requested_integrity(integrity: str | None) -> str:
    from llm_wiki import pyright_configuration

    if integrity is None:
        return pyright_configuration.PYRIGHT_PACKAGE_INTEGRITY
    return integrity


def _pyright_package_root(destination: Path, managed: bool) -> Path:
    if managed:
        return destination / "package"
    return destination / "node_modules/pyright"


def _write_pyright_package(
    package_root: Path, package_name: str, package_version: str, server_bytes: bytes
) -> Path:
    package_root.mkdir(parents=True, exist_ok=True)
    server = package_root / "langserver.index.js"
    server.write_bytes(server_bytes)
    (package_root / "package.json").write_bytes(
        canonical_json_bytes({"name": package_name, "version": package_version})
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
