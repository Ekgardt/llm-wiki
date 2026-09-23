"""The evidence generation is the only index; without one a search reads Markdown.

The legacy FTS5 index and vector cache were retired on 2026-09-23. See
`docs/research/2026-09-23-the-generation-is-the-only-index.md`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import doctor  # noqa: E402
import lookup_mode  # noqa: E402
import scheduled_nightly  # noqa: E402
import search_memory  # noqa: E402

from tests.test_doctor import _build_root, _check  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
LEGACY_NAMES = ("index.sqlite", "vectors_meta.json", ".paths-manifest", "_legacy_lexical_hits", "_legacy_dense_hits")


def _vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **pages: str) -> Path:
    notes = tmp_path / "vault" / "knowledge" / "notes"
    notes.mkdir(parents=True)
    for name, body in pages.items():
        (notes / f"{name}.md").write_text(body, encoding="utf-8")
    monkeypatch.setattr(search_memory, "ROOT", tmp_path / "vault")
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)
    monkeypatch.setattr(search_memory, "_active_generation_catalog", lambda: None)
    return notes


def test_no_script_reads_the_legacy_index_or_vector_cache() -> None:
    pattern = re.compile("|".join(re.escape(name) for name in LEGACY_NAMES))
    readers = sorted(
        path.name for path in (REPO / "scripts").glob("*.py") if pattern.search(path.read_text(encoding="utf-8"))
    )
    assert readers == []
    assert not any("search" == step.label for step in scheduled_nightly._post_compile_steps())


def test_markdown_hits_say_why_and_keep_the_named_page_first(tmp_path: Path, monkeypatch) -> None:
    _vault(
        tmp_path,
        monkeypatch,
        **{"needle-page": "# Needle Page\nA needle here.\n", "other": "# Other\nThe needle again, twice needle.\n"},
    )

    hits = search_memory.markdown_hits("needle page")

    assert [hit["path"] for hit in hits][0] == "knowledge/notes/needle-page.md"
    assert {hit["fallback_reason"] for hit in hits} == {"no_active_generation"}
    assert all(hit["partial"] is True for hit in hits)
    assert search_memory.markdown_hits("absent term") == []
    assert search_memory.markdown_hits("") == []


def test_a_named_archived_page_is_recalled_and_labelled(tmp_path: Path, monkeypatch) -> None:
    _vault(
        tmp_path,
        monkeypatch,
        **{"old-plan": "---\nstatus: archived\n---\n# Old Plan\nGone from the walk.\n"},
    )

    hits = search_memory.markdown_hits("old plan")

    assert [(hit["path"], hit.get("retired")) for hit in hits] == [("knowledge/notes/old-plan.md", True)]


def test_a_search_without_a_generation_reports_the_markdown_read(tmp_path: Path, monkeypatch) -> None:
    _vault(tmp_path, monkeypatch, page="# Page\nNeedle content.\n")

    results = search_memory.search("Needle content", graph=False, rerank=False, emit_telemetry=False, semantic=False)

    assert results and results[0]["fallback_reason"] == "no_active_generation"
    assert results[0]["path"] == "knowledge/notes/page.md"


def test_doctor_names_a_missing_generation_and_the_explicit_rebuild_builds_it(tmp_path: Path, monkeypatch) -> None:
    root, state_root, home = _build_root(tmp_path)
    (root / "knowledge" / "notes" / "page.md").write_text("# Page\nOne page.\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "_pyright_check", lambda *args, **kwargs: doctor._result("pyright", "ok", "ok", {}))

    before = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "generation")
    repaired = doctor.run_doctor(
        root=root, state_root=state_root, home=home, repair=True, rebuild_generation=True, time_budget_seconds=120
    )
    after = _check(repaired, "generation")

    assert (before["status"], before["details"]["repairable"], before["details"]["recommended_action"]) == (
        "degraded", True, "rebuild_generation",
    )
    assert (state_root / "cache" / "evidence-graph" / "catalog.sqlite3").is_file()
    assert after["status"] == "ok", after


def test_lookup_mode_names_the_generation_or_its_absence(monkeypatch) -> None:
    monkeypatch.setattr(search_memory, "_active_generation_catalog", lambda: None)
    assert lookup_mode.index_status() == {"available": False}

    class _Catalog:
        def get_active(self):
            return {"generation_id": "generation-9", "vector_state": "absent"}

    monkeypatch.setattr(search_memory, "_active_generation_catalog", lambda: _Catalog())
    assert lookup_mode.index_status() == {"available": True, "generation_id": "generation-9", "vector_state": "absent"}
