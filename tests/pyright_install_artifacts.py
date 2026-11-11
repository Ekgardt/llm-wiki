"""A synthetic npm-style Pyright tarball and the profile identity that approves it."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from reliable_memory import canonical_json_bytes


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
    with (
        destination.open("xb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tar_format) as archive,
    ):
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
    from llm_wiki import pyright_configuration

    monkeypatch.setattr(pyright_configuration, "PYRIGHT_PACKAGE_SHA256", artifact.package_sha256)
    monkeypatch.setattr(
        pyright_configuration, "PYRIGHT_PACKAGE_INTEGRITY", artifact.package_integrity
    )
