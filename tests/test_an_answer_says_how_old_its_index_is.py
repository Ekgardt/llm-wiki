"""An answer says how old its index is, and the index follows the compile.

A page compiled at 11:20 on 2026-09-24 was not found for the rest of the day
while `recall` said `fresh`; lookup_mode recommended a mode nothing used; doctor
was degraded most of every day by daily-log appends. See
docs/research/2026-09-24-an-answer-says-how-old-its-index-is.md.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import compile_memory  # noqa: E402
import doctor  # noqa: E402
import lookup_mode  # noqa: E402
import mcp_server  # noqa: E402
import memory_state  # noqa: E402
import search_memory  # noqa: E402


def _vault(tmp_path: Path, monkeypatch, *, page_after_build: bool) -> str:
    root, state = tmp_path / "vault", tmp_path / "state"
    notes = root / "knowledge" / "notes"
    notes.mkdir(parents=True)
    generation = "generation-test"
    manifest = state / "cache/evidence-graph/generations" / generation / "manifest.json"
    manifest.parent.mkdir(parents=True)
    page = notes / "page.md"
    page.write_text("# Page\n", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    built = time.time() - 60
    os.utime(manifest, (built, built))
    page_time = built + 30 if page_after_build else built - 30
    os.utime(page, (page_time, page_time))
    # The directory counts too since a removed page moves only it (audit B-19).
    os.utime(notes, (page_time, page_time))
    monkeypatch.setattr(memory_state, "ROOT", root)
    monkeypatch.setattr(memory_state, "STATE_ROOT", state)
    return generation


def _recall_data(generation: str, mode: str = "HYBRID") -> dict:
    return {
        "retrieval_trace": {
            "corpus_generation": generation,
            "requested_mode": mode,
            "signals_used": ["lexical", "dense"],
            "reranker_applied": True,
        }
    }


def test_a_page_written_after_the_index_makes_the_answer_stale(tmp_path, monkeypatch):
    generation = _vault(tmp_path, monkeypatch, page_after_build=True)

    components = mcp_server._recall_components(_recall_data(generation))

    assert (components["lexical"]["freshness"], components["dense"]["freshness"]) == ("stale", "stale")


def test_an_index_no_page_has_moved_past_is_fresh(tmp_path, monkeypatch):
    generation = _vault(tmp_path, monkeypatch, page_after_build=False)

    components = mcp_server._recall_components(_recall_data(generation))

    assert components["lexical"]["freshness"] == "fresh"


def test_graph_is_not_reported_missing_when_the_mode_never_asked_for_it(tmp_path, monkeypatch):
    generation = _vault(tmp_path, monkeypatch, page_after_build=False)

    hybrid = mcp_server._recall_components(_recall_data(generation, "HYBRID"))
    graph = mcp_server._recall_components(_recall_data(generation, "GRAPH"))

    assert ("graph" in hybrid, graph["graph"]["freshness"]) == (False, "missing")


def test_the_envelope_names_when_its_index_was_built(tmp_path, monkeypatch):
    generation = _vault(tmp_path, monkeypatch, page_after_build=False)

    stamp = mcp_server._index_timestamp("recall", _recall_data(generation))

    assert (stamp is not None, stamp.endswith("+00:00")) == (True, True)
    assert mcp_server._index_timestamp("vault_status", {}) is None


def test_lookup_mode_reports_the_mode_search_runs_in():
    modes = (
        lookup_mode.search_mode({"available": False}),
        lookup_mode.search_mode({"available": True, "vector_state": "absent"}),
        lookup_mode.search_mode({"available": True, "vector_state": "complete"}),
    )

    assert modes == ("DIRECT", "BASE", "HYBRID")


def _facts(delta: int) -> doctor._GenerationFacts:
    return doctor._GenerationFacts(delta, 0, 0, "current", "current", "current")


def test_doctor_calls_a_generation_stale_only_when_changed_sources_waited_a_day():
    day = doctor.GENERATION_FRESH_SECONDS
    verdicts = (
        doctor._generation_is_stale(_facts(5), 3600, True),
        doctor._generation_is_stale(_facts(0), day * 3, True),
        doctor._generation_is_stale(_facts(5), day + 1, True),
    )

    assert verdicts == (False, False, True)


def test_a_cli_hit_shows_its_text_not_only_its_heading():
    hit = {"summary": "Consequences", "content": "## Consequences\n\nThe index is the only one.\n"}

    assert search_memory.result_snippet(hit) == "The index is the only one."
    assert search_memory.result_snippet({"summary": "Only", "content": "# Only"}) == "Only"


def test_a_successful_compile_refreshes_the_generation(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(sys, "argv", ["compile_memory.py"])
    monkeypatch.setattr(compile_memory, "_compile_under_lock", lambda _args: 0)
    monkeypatch.setattr(
        doctor,
        "run_generation_maintenance",
        lambda _root, _state, *, time_budget_seconds: calls.append(time_budget_seconds) or {"status": "current"},
    )

    assert (compile_memory.main(), calls) == (0, [compile_memory.POST_COMPILE_GENERATION_SECONDS])


def test_a_failed_or_dry_compile_does_not_refresh(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(compile_memory, "_compile_under_lock", lambda _args: 1)
    monkeypatch.setattr(compile_memory, "_refresh_generation_after_compile", lambda: calls.append(1))

    monkeypatch.setattr(sys, "argv", ["compile_memory.py"])
    failed = compile_memory.main()
    monkeypatch.setattr(compile_memory, "_compile_under_lock", lambda _args: 0)
    monkeypatch.setattr(sys, "argv", ["compile_memory.py", "--dry-run"])
    dry = compile_memory.main()

    assert (failed, dry, calls) == (1, 0, [])
