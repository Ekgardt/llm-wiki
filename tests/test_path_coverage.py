"""CODE-05: a path's coverage answer is honest about index, freshness and bounds."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import path_coverage  # noqa: E402


def test_freshness_names_all_four_states(tmp_path: Path) -> None:
    (tmp_path / "present.py").write_text("x = 1\n", encoding="utf-8")
    current = path_coverage._current_sha(tmp_path, "present.py")
    assert path_coverage._freshness(current, current) == "fresh"
    assert path_coverage._freshness("0" * 64, current) == "stale"
    assert path_coverage._freshness(None, current) == "not_indexed"
    assert path_coverage._freshness(current, None) == "missing_on_disk"


def test_an_overflowing_node_count_is_marked_inexact() -> None:
    class _CeilingGraph:
        def find_nodes(self, **_kwargs):
            raise ValueError("Evidence Graph query row ceiling exceeded")

    counted = path_coverage._node_count(_CeilingGraph(), "any.py", 0.0)
    assert counted == {
        "nodes": path_coverage.NODE_CEILING,
        "nodes_exact": False,
    }


def test_a_stored_source_and_its_parse_are_answered_from_the_generation() -> None:
    """Issue #24, B1: no manifest lookup — the source row is the answer."""

    class _Graph:
        generation_id = "generation-x"

        def source_by_path(self, relative, *, deadline=None):
            return {"sha256": "0" * 64, "size": 5, "language": "python", "content": b"x = 1"}

        def source_observations(self, relative, *, deadline=None):
            return {}

        def find_nodes(self, **_kwargs):
            return [{}]

    answer = path_coverage._coverage_answer(_Graph(), Path("/nonexistent"), "a.py", 0.0)
    expected = {"indexed": True, "freshness": "missing_on_disk", "nodes": 1}
    assert {key: answer[key] for key in expected} == expected
    assert answer["parse"]["status"] == "ok"
    assert not hasattr(path_coverage, "_source_manifest")


def test_the_note_admits_the_limit() -> None:
    assert "not proof of completeness" in path_coverage.COVERAGE_NOTE
