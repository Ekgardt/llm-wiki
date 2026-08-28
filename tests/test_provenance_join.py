"""MEM-16: the symbol -> decision -> source chain answers, and never invents."""

from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import provenance_join  # noqa: E402


def _vault(tmp_path: Path) -> Path:
    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "why-the-widget-decision.md").write_text(
        "# Why The Widget\n\n"
        "The `frobnicate_widget` helper exists because of the 2026 outage.\n\n"
        "## Source\n\n"
        "- knowledge/daily/2026-08-01.md\n"
        "- knowledge/raw/sessions/2026-08-01/session-a.md\n",
        encoding="utf-8",
    )
    (notes / "unrelated.md").write_text(
        "# Unrelated\n\nNothing about widgets here.\n", encoding="utf-8"
    )
    return tmp_path


def _join(vault: Path, symbol: str) -> dict:
    return provenance_join.join_symbol_provenance(
        vault, vault, symbol, time.monotonic() + 10
    )


def test_a_named_symbol_reaches_its_decision_and_sources(tmp_path: Path) -> None:
    result = _join(_vault(tmp_path), "frobnicate_widget")
    assert [page["slug"] for page in result["pages"]] == ["why-the-widget-decision"]
    page = result["pages"][0]
    assert "knowledge/daily/2026-08-01.md" in page["cited_sources"]
    assert (
        "knowledge/raw/sessions/2026-08-01/session-a.md" in page["cited_sources"]
    )
    assert "not re-verified" in result["verification"]


def test_an_unknown_symbol_gets_an_empty_answer_not_an_invented_one(
    tmp_path: Path,
) -> None:
    result = _join(_vault(tmp_path), "no_such_symbol_anywhere")
    assert result["pages"] == []
    assert result["locations"] == []


def test_word_boundaries_keep_substrings_out(tmp_path: Path) -> None:
    """`widget` alone must not match `frobnicate_widget`'s page by substring."""
    result = _join(_vault(tmp_path), "frobnicate")
    assert result["pages"] == []


def test_the_scan_is_bounded_by_its_deadline(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    expired = provenance_join.join_symbol_provenance(
        vault, vault, "frobnicate_widget", time.monotonic() - 1
    )
    assert expired["pages"] == []
