"""A managed toolchain is run before it is published and re-checked on a re-run (audit 2026-09-26 C-9).

docs/research/2026-09-26-a-toolchain-is-proven-before-it-is-published.md
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import lsp_profiles
import pytest
from install_language_server import (
    INSTALL_MANIFEST_NAME,
    InstallError,
    _prove_toolchain,
    _validated_existing_install,
)
from lsp_identity import build_install_manifest

RUST = lsp_profiles.REGISTRY.get("rust-analyzer")
posix_only = pytest.mark.skipif(os.name == "nt", reason="the fake toolchain is a POSIX shell script")


def _executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _toolchain(root: Path, exit_code: int = 0) -> None:
    for relative, _argument in RUST.toolchain_probes:
        _executable(root / relative, f"#!/bin/sh\n[ \"$1\" = --version ] || exit 9\nexit {exit_code}\n")


def _existing_install(root: Path) -> None:
    server = root / RUST.server_relative
    _executable(server, "#!/bin/sh\n")
    digest = hashlib.sha256(server.read_bytes()).hexdigest()
    manifest = build_install_manifest(RUST, server_sha256=digest)
    (root / INSTALL_MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")


@posix_only
def test_a_toolchain_that_runs_is_published(tmp_path) -> None:
    _toolchain(tmp_path)

    _prove_toolchain(RUST, tmp_path, deadline=float("inf"))


@posix_only
def test_a_toolchain_that_does_not_run_is_refused(tmp_path) -> None:
    _toolchain(tmp_path, exit_code=1)

    with pytest.raises(InstallError):
        _prove_toolchain(RUST, tmp_path, deadline=float("inf"))


@posix_only
def test_a_rerun_refuses_an_install_whose_toolchain_is_gone(tmp_path) -> None:
    _existing_install(tmp_path)
    _toolchain(tmp_path)
    kept = _validated_existing_install(RUST, tmp_path)
    (tmp_path / RUST.toolchain_probes[0][0]).unlink()

    with pytest.raises(InstallError):
        _validated_existing_install(RUST, tmp_path)
    assert kept == tmp_path


def _puts_a_toolchain_on_path(name: str) -> bool:
    return "PATH" in dict(lsp_profiles.REGISTRY.get(name).environment_template)


def test_every_profile_that_puts_a_toolchain_on_path_proves_it() -> None:
    """A PATH in the launch environment means the server runs a tool; that tool is probed."""
    on_path = sorted(filter(_puts_a_toolchain_on_path, lsp_profiles.REGISTRY.names()))
    probes = [bool(lsp_profiles.REGISTRY.get(name).toolchain_probes) for name in on_path]

    assert (on_path, probes) == (["gopls", "rust-analyzer"], [True, True])
