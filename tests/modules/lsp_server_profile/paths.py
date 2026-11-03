"""Absolute fixture paths shared by value and integration tests."""
from pathlib import Path

# `Path("/srv/vault").is_absolute()` is False on Windows — a path needs a drive
# there — so every root handed to a profile has to be anchored to one. Eight
# Windows jobs failed on 2026-08-30 with "node must be an absolute path" and a
# comparison of "D:\\srv\\vault…" against "\\srv\\vault…".
ANCHOR = Path(Path.cwd().anchor)


def _abs(*parts: str) -> Path:
    """An absolute path on every platform these tests run on."""
    return ANCHOR.joinpath(*parts)


STATE_ROOT = _abs("srv", "vault")
