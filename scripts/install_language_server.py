"""Install one profile's pinned language server. A separate, explicit action.

`knowledge/notes/read-only-lsp-navigation-engine-decision.md` makes installation
an operator action, never something a navigation query triggers. This module is
that action for any profile in `lsp_profiles.REGISTRY` other than Pyright, which
keeps its own installer (`scripts/install_pyright.py`) with its own lock,
ownership and rollback machinery.

It is deliberately smaller than that one and says so. It downloads, verifies and
unpacks; it does not take an install lock, does not survive a concurrent
installer, and does not roll back a partially written tree beyond removing its
own staging directory. What it does not compromise on is what the bytes are: the
tarball must match the profile's pinned Subresource Integrity hash before a
single member is read, every member is bounded and contained, and nothing but
regular files and directories is written.

Two artifacts, not one. `typescript-language-server` resolves its TypeScript at
runtime and a managed install carries no `node_modules`, so the profile names a
second pinned tarball on its `RuntimeOption` and this installer unpacks it as a
sibling. See `docs/research/2026-08-28-precise-navigation-beyond-python.md`,
Finding 3.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from lsp_identity import INSTALL_MANIFEST_NAME, build_install_manifest
from lsp_profiles import REGISTRY
from lsp_server_profile import LanguageServerProfile, RuntimeOption
from reliable_memory import canonical_json_bytes

NETWORK_TIMEOUT_SECONDS = 60.0
MAX_COMPRESSED_BYTES = 32 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 8192
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_PATH_COMPONENTS = 64

# Every npm tarball roots its payload at `package/`. The server profile's
# `server_relative` includes that component and is extracted as-is; the runtime
# artifact is re-rooted under the directory its `sibling_relative` names.
NPM_ROOT = "package"


class InstallError(RuntimeError):
    """The install could not be completed as pinned."""


def _integrity_digest(integrity: str) -> bytes:
    return base64.b64decode(integrity.split("-", 1)[1], validate=True)


def _require_integrity(content: bytes, integrity: str, label: str) -> None:
    if hashlib.sha512(content).digest() != _integrity_digest(integrity):
        raise InstallError(f"{label} does not match its pinned integrity hash")


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
        if response.geturl() != url:
            raise InstallError(f"{url} redirected; a pinned URL must be served directly")
        content = response.read(MAX_COMPRESSED_BYTES + 1)
    if len(content) > MAX_COMPRESSED_BYTES:
        raise InstallError(f"{url} exceeds the compressed bound")
    return content


def _artifact_bytes(url: str, integrity: str, local: Path | None, label: str) -> bytes:
    """The pinned tarball, from disk when the operator supplied it, else fetched."""
    content = _fetch(url) if local is None else local.read_bytes()
    _require_integrity(content, integrity, label)
    return content


def _member_path_escapes(path: Path) -> bool:
    return path.is_absolute() or any(part in {"..", "/"} for part in path.parts)


def _member_relative(member: tarfile.TarInfo) -> Path:
    path = Path(member.name)
    if not path.parts or len(path.parts) > MAX_PATH_COMPONENTS:
        raise InstallError(f"member path is unusable: {member.name!r}")
    if _member_path_escapes(path):
        raise InstallError(f"member path escapes the archive: {member.name!r}")
    return Path(*path.parts)


def _require_plain_member(member: tarfile.TarInfo) -> None:
    if not (member.isfile() or member.isdir()):
        raise InstallError(f"member is not a regular file or directory: {member.name!r}")
    if member.size > MAX_MEMBER_BYTES:
        raise InstallError(f"member exceeds its bound: {member.name!r}")


def _rerooted(relative: Path, subdirectory: str | None) -> Path | None:
    """Where a member lands, or None when it is outside the payload root."""
    if subdirectory is None:
        return relative
    if relative.parts[0] != NPM_ROOT:
        return None
    return Path(subdirectory, *relative.parts[1:])


def _extract_file(archive: tarfile.TarFile, member: tarfile.TarInfo, target: Path) -> int:
    source = archive.extractfile(member)
    if source is None:
        raise InstallError(f"member could not be read: {member.name!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with source, open(target, "wb") as handle:
        shutil.copyfileobj(source, handle, length=64 * 1024)
    return member.size


def _extract_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    root: Path,
    subdirectory: str | None,
) -> int:
    _require_plain_member(member)
    destination = _rerooted(_member_relative(member), subdirectory)
    if destination is None:
        return 0
    target = root / destination
    if member.isdir():
        target.mkdir(parents=True, exist_ok=True)
        return 0
    return _extract_file(archive, member, target)


def _extract(content: bytes, root: Path, subdirectory: str | None) -> None:
    """Unpack a bounded, contained tarball under `root`."""
    written = 0
    with tempfile.NamedTemporaryFile(suffix=".tgz") as scratch:
        scratch.write(content)
        scratch.flush()
        with tarfile.open(scratch.name, mode="r:gz") as archive:
            members = _bounded_members(archive)
            for member in members:
                written += _extract_member(archive, member, root, subdirectory)
                _require_total(written)


def _bounded_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if len(members) > MAX_MEMBERS:
        raise InstallError("archive exceeds the member bound")
    return members


def _require_total(written: int) -> None:
    if written > MAX_DECOMPRESSED_BYTES:
        raise InstallError("archive exceeds the decompressed bound")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _install_runtime(
    runtime: RuntimeOption | None, staging: Path, local: Path | None
) -> str:
    """Unpack the pinned engine beside the server; its digest, or empty."""
    if runtime is None:
        return ""
    if runtime.package_url is None or runtime.package_integrity is None:
        raise InstallError("profile names a runtime path with no pinned artifact")
    content = _artifact_bytes(
        runtime.package_url, runtime.package_integrity, local, "runtime artifact"
    )
    _extract(content, staging, runtime.install_subdirectory)
    return _sha256_file(staging / runtime.sibling_relative)


def _require_absent(root: Path) -> None:
    if root.exists():
        raise InstallError(
            f"{root} already exists; remove it deliberately before reinstalling"
        )


def _staged(
    profile: LanguageServerProfile, staging: Path, artifacts: tuple[Path | None, Path | None]
) -> dict[str, str]:
    server_artifact, runtime_artifact = artifacts
    content = _artifact_bytes(
        profile.package_url, profile.package_integrity, server_artifact, "server artifact"
    )
    _extract(content, staging, None)
    server_sha256 = _sha256_file(staging / profile.server_relative)
    runtime_sha256 = _install_runtime(profile.runtime_option, staging, runtime_artifact)
    return build_install_manifest(
        profile, server_sha256=server_sha256, runtime_sha256=runtime_sha256
    )


def install_language_server(
    profile: LanguageServerProfile,
    *,
    state_root: Path,
    server_artifact: Path | None = None,
    runtime_artifact: Path | None = None,
) -> Path:
    """Install the pinned artifacts and write the receipt; return the managed root."""
    root = profile.managed_root(state_root)
    _require_absent(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".install-", dir=root.parent))
    try:
        manifest = _staged(profile, staging, (server_artifact, runtime_artifact))
        (staging / INSTALL_MANIFEST_NAME).write_bytes(canonical_json_bytes(manifest))
        staging.rename(root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return root


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--profile", required=True, choices=REGISTRY.names())
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--runtime-artifact", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    profile = REGISTRY.get(arguments.profile)
    if profile.name == "pyright":
        print("use scripts/install_pyright.py for the Pyright profile", file=sys.stderr)
        return 2
    try:
        root = install_language_server(
            profile,
            state_root=arguments.state_root,
            server_artifact=arguments.artifact,
            runtime_artifact=arguments.runtime_artifact,
        )
    except (InstallError, OSError, tarfile.TarError, ValueError) as error:
        print(f"install failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"profile": profile.name, "root": str(root)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
