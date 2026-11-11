"""Helpers shared by test_corpus_snapshot.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
