"""A bound that drops items either keeps the data or says what it left out (law 9).

docs/research/2026-09-27-a-cut-says-what-it-left-out.md
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import code_graph  # noqa: E402
import episode_consolidation  # noqa: E402
import provenance_join  # noqa: E402
import repository_worktrees  # noqa: E402
import retrieval_disposition  # noqa: E402

NINE = range(9)


def test_every_grounded_lesson_of_a_batch_is_kept(tmp_path: Path) -> None:
    record = tmp_path / "session-a.md"
    record.write_text("\n".join(f"quote number {index} holds" for index in NINE), encoding="utf-8")
    items = [
        {"kind": "lesson", "text": f"lesson {index}", "quote": f"quote number {index} holds"}
        for index in NINE
    ]

    lessons = episode_consolidation.grounded_lessons(json.dumps(items), [record])

    assert len(lessons) == 9




def _notes(vault: Path, count: int, matching: set[int]) -> None:
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    for index in range(count):
        body = "names frobnicate_widget" if index in matching else "nothing here"
        (notes / f"note-{index:04d}.md").write_text(f"# Note {index}\n\n{body}\n", encoding="utf-8")


def _provenance(vault: Path) -> dict:
    return provenance_join.join_symbol_provenance(vault, vault, "frobnicate_widget", time.monotonic() + 30)


def test_a_note_past_the_five_hundredth_is_searched(tmp_path: Path) -> None:
    _notes(tmp_path, 510, {505})

    result = _provenance(tmp_path)

    assert [page["slug"] for page in result["pages"]] == ["note-0505"]
    assert result["scan_complete"] is True


def test_a_provenance_answer_says_how_many_pages_it_left_out(tmp_path: Path) -> None:
    _notes(tmp_path, 12, set(range(10)))

    result = _provenance(tmp_path)

    assert (len(result["pages"]), result["page_count"], result["pages_truncated"]) == (8, 10, True)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, timeout=60)


def test_a_worktree_past_the_sixty_fourth_is_discovered(tmp_path: Path) -> None:
    checkout = tmp_path / "repository"
    checkout.mkdir()
    _git(checkout, "init", "-q")
    _git(checkout, "-c", "user.email=t@example.test", "-c", "user.name=T", "commit", "-q", "--allow-empty", "-m", "i")
    for index in range(70):
        _git(checkout, "worktree", "add", "-q", "--detach", str(tmp_path / f"wt-{index:02d}"))

    assert len(repository_worktrees.list_worktrees(checkout)) == 71


class _ManyPaths:
    def path(self, source, target, **_options):
        return [{"from": source, "to": target, "hop": hop} for hop in range(code_graph.PATH_MAX_ROWS)]


def test_a_paths_answer_says_when_it_was_cut() -> None:
    paths, cut = code_graph._paths_between(_ManyPaths(), ["a", "b"], ["c"])

    assert (len(paths), cut) == (code_graph.PATH_MAX_ROWS, True)


def test_a_flow_answer_says_when_it_was_cut() -> None:
    rows = [{}] * (code_graph.FLOW_MAX_ROWS + 1)

    assert code_graph._flow_cut_report(rows, False) == {
        "flow_count": code_graph.FLOW_MAX_ROWS + 1,
        "flows_truncated": True,
    }


def test_the_disposition_printout_says_how_many_more(capsys) -> None:
    counts = {f"page-{index}": 1 for index in range(retrieval_disposition.MAX_PRINTED_PAGES + 3)}

    retrieval_disposition._print_counts("Refused", counts)

    assert capsys.readouterr().out.rstrip().endswith("… and 3 more")
