"""Pure path derivation for the managed read-only LSP runtime."""
from __future__ import annotations

import re
from pathlib import Path

PYRIGHT_VERSION = "1.1.411"
PYRIGHT_RELATIVE_ROOT = Path("cache/code-tools/pyright") / PYRIGHT_VERSION
LSP_RELATIVE_ROOT = Path("run/lsp")


def _resolved_state_root(state_root: Path) -> Path:
    if not isinstance(state_root, Path):
        raise TypeError("state_root must be a Path")
    return state_root.resolve()


def managed_pyright_root(state_root: Path) -> Path:
    """Derive the pinned managed Pyright root without creating it."""
    return _resolved_state_root(state_root) / PYRIGHT_RELATIVE_ROOT


def lsp_owner_root(state_root: Path, owner_nonce: str) -> Path:
    """Derive one process owner's LSP scratch root without creating it."""
    if not isinstance(owner_nonce, str):
        raise TypeError("owner_nonce must be a string")
    if re.fullmatch(r"[0-9a-f]{32}", owner_nonce) is None:
        raise ValueError("owner_nonce must be 32 lowercase hexadecimal characters")
    return _resolved_state_root(state_root) / LSP_RELATIVE_ROOT / owner_nonce
