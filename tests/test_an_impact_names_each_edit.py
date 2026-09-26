"""An impact names each edit, from any folder, on any number of files (audit 2026-09-26 B-8).

docs/research/2026-09-26-an-impact-names-each-edit.md
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import impact_analysis

OLD = b"".join(f"def f{n}():\n    return {n}\n\n".encode() for n in range(10))


def test_two_edits_are_two_ranges_and_the_lines_between_are_not_changed() -> None:
    new = OLD.replace(b"return 1\n", b"return 100\n").replace(b"return 8\n", b"return 800\n")

    ranges = impact_analysis._changed_ranges(OLD, new)

    assert [(item["new"]["line_start"], item["new"]["line_end"]) for item in ranges] == [(5, 5), (26, 26)]


def test_the_top_of_the_repository_is_found_from_a_subfolder(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "pkg").mkdir()

    top = impact_analysis.repository_top((tmp_path / "pkg").resolve(), float("inf"))

    assert top == tmp_path.resolve()


class _Graph:
    def __init__(self) -> None:
        self.asked: list[dict] = []

    def find_nodes(self, **kwargs):
        self.asked.append(kwargs)
        return [{"node_id": "file:1", "metadata": {"value": "pkg/a.py"}}]


def test_changed_files_are_asked_for_by_value_not_read_whole() -> None:
    graph = _Graph()
    changes = [{"old_path": "pkg/a.py", "new_path": "pkg/a.py"}]

    found = impact_analysis._project_file_ids(graph, changes, impact_analysis.ImpactLimits(), float("inf"))

    assert (found, graph.asked[0]["values"]) == ({"file:1"}, ["pkg/a.py", "pkg\\a.py"])
