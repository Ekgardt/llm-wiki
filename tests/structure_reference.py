"""The structure reference is one document kept in several files."""
from __future__ import annotations

from pathlib import Path

STRUCTURE_REFERENCE_FILES = (
    'docs/STRUCTURE.md',
    'docs/contracts/compile-and-nightly.md',
    'docs/contracts/integration-and-recovery.md',
    'docs/contracts/reliability-and-capture.md',
    'docs/contracts/corpus-and-navigation.md',
    'docs/MODULES.md',
)


def structure_reference(root: Path) -> str:
    return '\n'.join((root / name).read_text(encoding='utf-8') for name in STRUCTURE_REFERENCE_FILES)
