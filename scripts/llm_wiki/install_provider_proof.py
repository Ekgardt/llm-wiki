"""Proof that an installed Codex provider bundle was read whole."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from install_state import (
    PROVIDER_READ_PROOF,
    InstallControlError,
    read_state_bytes,
    read_v2_snapshot,
    validated_install_record,
)
from llm_wiki import install_provider_files
from llm_wiki.install_provider_files import (
    provider_handle_identity,
    qualified_codex_desired_record,
    require_provider_identity,
)


@dataclass
class _ProviderReadProof:
    stack: ExitStack
    reads: list = field(default_factory=list)

    def read(self, path, limit):
        resolved = path.resolve(strict=True)
        handle = install_provider_files.provider_open(self.stack, path)
        identity = provider_handle_identity(handle)
        raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise InstallControlError("install_provider_snapshot_changed")
        require_provider_identity(provider_handle_identity(handle), identity)
        require_provider_identity(path.resolve(strict=True), resolved)
        self.reads.append((path, resolved, handle, identity, raw))
        return raw

    def verify(self):
        for record in self.reads:
            _verify_provider_read(record)


def _verify_provider_read(record):
    path, resolved, original, identity, raw = record
    require_provider_identity(path.resolve(strict=True), resolved)
    with ExitStack() as stack:
        reopened = install_provider_files.provider_open(stack, path)
        require_provider_identity(provider_handle_identity(reopened), identity)
        require_provider_identity(reopened.read(len(raw) + 1), raw)
        require_provider_identity(provider_handle_identity(reopened), identity)
        require_provider_identity(provider_handle_identity(original), identity)
        require_provider_identity(path.resolve(strict=True), resolved)


@contextmanager
def provider_snapshot_proof():
    with ExitStack() as stack:
        proof = _ProviderReadProof(stack)
        token = PROVIDER_READ_PROOF.set(proof)
        try:
            yield
            proof.verify()
        finally:
            PROVIDER_READ_PROOF.reset(token)


def installed_codex_provider_bundle(root: Path, state_root: Path, home: Path) -> dict[str, str]:
    """Hold fresh proof-file identities until the entire desired chain is validated."""
    with provider_snapshot_proof():
        return _installed_codex_provider_bundle(root, state_root, home)


def _installed_codex_provider_bundle(root, state_root, home):
    install_root = state_root.resolve() / "run/install"
    manifest_raw = read_state_bytes(install_root / "manifest.json")
    transaction_raw = read_state_bytes(install_root / "transaction.json")
    manifest = validated_install_record(manifest_raw, "install-manifest/")
    transaction = validated_install_record(transaction_raw, "install-transaction/")
    record = qualified_codex_desired_record(install_root, manifest, transaction, root, state_root, home)
    desired = read_v2_snapshot(install_root, record["desired"])
    return install_provider_files.codex_desired_bundle(desired, root)
