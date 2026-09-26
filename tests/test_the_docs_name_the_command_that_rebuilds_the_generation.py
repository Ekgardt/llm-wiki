"""The docs name `--rebuild-generation` wherever they tell how to rebuild the generation.

`doctor --repair` only repairs the generation catalog; the guide said it rebuilt
the generation. See docs/research/2026-09-25-the-docs-name-the-rebuild-command.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = sorted([*ROOT.glob("README*.md"), *(ROOT / "docs").glob("*.md")])
REBUILD_WORDS = ("rebuild the generation", "generation refresh")


def _paragraphs(path: Path) -> list[str]:
    return [" ".join(block.split()) for block in path.read_text(encoding="utf-8").split("\n\n")]


def _misleading(paragraph: str) -> bool:
    if "doctor.py --repair" not in paragraph or "--rebuild-generation" in paragraph:
        return False
    return any(words in paragraph.lower() for words in REBUILD_WORDS)


@pytest.mark.parametrize("path", DOCS, ids=lambda path: path.name)
def test_no_paragraph_says_repair_rebuilds_the_generation(path: Path) -> None:
    assert [paragraph[:120] for paragraph in _paragraphs(path) if _misleading(paragraph)] == []
