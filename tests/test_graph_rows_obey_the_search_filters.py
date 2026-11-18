"""A page the evidence graph adds answers to the same filters as a page found by words.

Every search asks the graph since 2026-11-18, so a graph neighbour from another
project, outside the asked dates, or superseded would otherwise reach any
answer. See docs/research/2026-11-17-every-search-runs-every-leg.md.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import graph_neighbors  # noqa: E402
import retrieval  # noqa: E402
import search_memory  # noqa: E402

FILTERS = {"project": "alpha", "since": "2025-01-01", "as_of": None, "scope": "all"}

# path -> (project, valid_from, status); only the first passes FILTERS.
PAGES = {
    "knowledge/notes/kept.md": ("alpha", "2026-05-01", ""),
    "knowledge/notes/other-project.md": ("beta", "2026-05-01", ""),
    "knowledge/notes/too-old.md": ("alpha", "2024-01-01", ""),
    "knowledge/notes/superseded.md": ("alpha", "2026-05-01", "superseded"),
}


def _generation_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(search_memory._GENERATION_FTS_DDL)
    for path, (project, valid_from, status) in PAGES.items():
        connection.execute(
            "INSERT INTO chunks(source_path, project, valid_from, status, content)"
            " VALUES (?, ?, ?, ?, 'neighbour')",
            (path, project, valid_from, status),
        )
    return connection


def _graph_row(path: str) -> dict[str, object]:
    return {"candidate_id": path, "relative_path": path, "seed_id": "seed", "hop": 1}


def test_a_generation_graph_row_obeys_project_dates_and_status() -> None:
    rows = [_graph_row(path) for path in PAGES]

    admitted = retrieval._graph_rows_admitted(rows, FILTERS, _generation_connection())

    assert [row["relative_path"] for row in admitted] == ["knowledge/notes/kept.md"]


def test_a_generation_graph_row_keeps_every_page_without_filters() -> None:
    rows = [_graph_row(path) for path in PAGES if "superseded" not in path]

    admitted = retrieval._graph_rows_admitted(rows, {}, _generation_connection())

    assert admitted == rows


def _write_pages(root: Path) -> None:
    for path, (project, valid_from, status) in PAGES.items():
        page = root / path
        page.parent.mkdir(parents=True, exist_ok=True)
        status_line = f"status: {status}\n" if status else ""
        page.write_text(
            f"---\ntype: concept\nproject: {project}\ntimestamp: {valid_from}\n"
            f"{status_line}---\n# {page.stem}\n\nOne-sentence summary: neighbour.\n",
            encoding="utf-8",
        )


def _legacy_neighbours(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seen: dict[str, object]
) -> list[str]:
    _write_pages(tmp_path)
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)

    def boost(
        bm25_results: list[dict[str, object]],
        vector_results: list[dict[str, object]] | None,
        boost_weight: float = 0.15,
        *,
        deadline: float | None = None,
    ) -> list[dict[str, object]]:
        seen.update(seeds=bm25_results, deadline=deadline)
        return [{"path": path, "graph_boost": 0.15} for path in PAGES]

    monkeypatch.setattr(graph_neighbors, "boost_graph_neighbors", boost)
    filters = {
        **FILTERS,
        "seeds": [{"relative_path": "knowledge/notes/seed.md", "score": 1.0}],
        "deadline_monotonic": 123.0,
    }
    return [str(row["relative_path"]) for row in retrieval._neighbour_boost_hits(filters)]


def test_a_markdown_neighbour_obeys_project_dates_and_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    neighbours = _legacy_neighbours(monkeypatch, tmp_path, {})

    assert neighbours == ["knowledge/notes/kept.md"]


def test_the_markdown_walk_reuses_the_seeds_and_the_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    _legacy_neighbours(monkeypatch, tmp_path, seen)

    assert seen["seeds"] == [{"path": "knowledge/notes/seed.md", "score": 1.0}]
    assert seen["deadline"] == pytest.approx(123.0)


def test_the_link_graph_build_stops_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    def walk(*_args: object, deadline: float | None = None, **_kwargs: object) -> None:
        seen.append(deadline)
        raise TimeoutError("deadline")

    seen: list[object] = []
    monkeypatch.setattr(graph_neighbors, "get_link_graph", walk)

    with pytest.raises(TimeoutError):
        graph_neighbors.boost_graph_neighbors([{"path": "a.md", "score": 1.0}], None, deadline=5.0)
    assert seen == [5.0]


class _Graph:
    generation_id = "gen-filters"

    def close(self) -> None:
        return None

    @classmethod
    def open_active_for_repository(cls, *_args: object, **_kwargs: object) -> _Graph:
        return cls()


def _catalog(tmp_path: Path) -> object:
    from repository_scope import resolve_repository_scope

    manifest = {
        "generation_id": "gen-filters",
        "repository_scope": resolve_repository_scope(search_memory.ROOT).as_dict(),
        "vector_state": "absent",
        "artifacts": [
            {"path": "search.sqlite3", "size": 1, "sha256": "0" * 64},
            {"path": "evidence.sqlite3", "size": 1, "sha256": "1" * 64},
        ],
    }

    class Catalog:
        generations_path = tmp_path / "generations"
        state_root = tmp_path

        def get_active_for_repository(
            self, _repository_scope: object, **_kwargs: object
        ) -> dict[str, object]:
            return manifest

    return Catalog()


def _seed_hit() -> dict[str, object]:
    return {
        "candidate_id": "seed",
        "parent_id": "knowledge/notes/seed.md",
        "relative_path": "knowledge/notes/seed.md",
        "source_sha256": "a" * 64,
        "byte_start": 0,
        "byte_end": 10,
        "score": 1.0,
        "title": "seed",
        "content": "capture queue",
        "project": "alpha",
        "retrieval_confidence": "high",
    }


def _neighbour_row(path: str) -> dict[str, object]:
    step = {
        "assertion_id": f"assertion:{path}",
        "source_node_id": "seed",
        "target_node_id": path,
        "edge_type": "CALLS",
        "direction": "out",
        "evidence_ids": (f"evidence:{path}",),
    }
    return {
        **_seed_hit(),
        **_graph_row(path),
        "parent_id": path,
        "project": "",
        "content": "capture queue neighbour",
        "edge_type": "CALLS",
        "direction": "out",
        "assertion_path": (step,),
        "evidence_ids": step["evidence_ids"],
    }


def _neighbour_rows(**_options: object) -> tuple[dict[str, object], ...]:
    return tuple(_neighbour_row(path) for path in PAGES)


def test_a_search_for_one_project_gets_no_graph_page_of_another(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import evidence_graph

    monkeypatch.setattr(search_memory, "_generation_consumption_seal", lambda *_a: ("seal",))
    monkeypatch.setattr(search_memory, "_generation_consumption_unchanged", lambda *_a: True)
    monkeypatch.setattr(
        search_memory, "_generation_connection", lambda *_a: _generation_connection()
    )
    monkeypatch.setattr(search_memory, "_generation_fts_search", lambda *_a, **_k: [_seed_hit()])
    monkeypatch.setattr(evidence_graph, "EvidenceGraph", _Graph)
    monkeypatch.setattr(
        retrieval, "expand_evidence_graph", lambda _graph, **_options: _neighbour_rows()
    )

    rows = retrieval.retrieve_via_search_memory(
        "what depends on the capture queue",
        catalog=_catalog(tmp_path),
        semantic=True,
        graph=True,
        rerank=False,
        emit_telemetry=False,
        project="alpha",
        since="2025-01-01",
    )

    graph_pages = {row["candidate_id"] for row in rows} - {"seed"}
    assert graph_pages == {"knowledge/notes/kept.md"}
