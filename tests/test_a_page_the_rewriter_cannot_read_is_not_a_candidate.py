"""The finder and the rewriter of the reflection pass read a page the same way.

Research: `docs/research/2026-09-17-a-page-the-rewriter-cannot-read-is-not-a-candidate.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import reflection  # noqa: E402

PAGE = (
    b"---\ntype: concept\n---\n# Timers\n\nOne-sentence summary: Timers beat cron.\n\n"
    b"## Update (2026-08-01)\nfirst\n\n## Update (2026-08-02)\nsecond\n"
)


@pytest.fixture
def notes(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "knowledge" / "notes"
    directory.mkdir(parents=True)
    monkeypatch.setattr(reflection, "KNOWLEDGE", directory)
    (directory / "good.md").write_bytes(PAGE)
    return directory


def _candidate_slugs() -> list[str]:
    return [candidate["slug"] for candidate in reflection.find_reflection_candidates()]


def test_a_page_with_a_byte_that_is_not_utf8_is_not_offered_to_the_rewriter(notes):
    (notes / "broken.md").write_bytes(PAGE + b"\xff\xfe stray bytes\n")

    assert _candidate_slugs() == ["good"]


def test_a_page_over_the_rewriters_bound_is_not_offered_to_the_rewriter(notes, monkeypatch):
    (notes / "huge.md").write_bytes(PAGE + b"x" * 4096)
    monkeypatch.setattr(reflection, "MAX_REFLECTION_PAGE_BYTES", len(PAGE) + 100)

    assert _candidate_slugs() == ["good"]
