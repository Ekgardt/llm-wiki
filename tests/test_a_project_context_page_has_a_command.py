"""The only writer of `context.md` is named where a person can find it.

Finding M-C2 of the third audit. See
`docs/research/2026-09-18-the-project-context-page-gets-its-command-back.md`.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILDER = ROOT / "scripts/build_context.py"
GUIDE = ROOT / "docs/USER-GUIDE.md"


def test_the_builder_and_its_documented_command_stand_or_fall_together() -> None:
    """A module no document names is dead again, whatever this decision said."""
    guide = GUIDE.read_text(encoding="utf-8")

    assert (BUILDER.is_file(), "scripts/build_context.py --slug" in guide) == (True, True)


def test_the_documented_command_writes_the_page_the_layout_names() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    builder = BUILDER.read_text(encoding="utf-8")

    assert "knowledge/projects/<slug>/context.md" in guide
    assert '"context.md"' in builder
