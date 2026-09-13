"""The parity gold must still resolve against the tree it describes.

The gold in `benchmark/code-parity-v2.json` is hand-read fact: a path, a line,
and the name that line defines or calls. Code moves; on 2026-09-12 every line
number in that file except the two data-flow tasks pointed at the 2026-08-28
tree, one task asked about a symbol the H1 deletion had removed, and five tasks
carried a line number as a graded term that nothing in the tree could match —
so every side would have graded wrong whatever it answered. A stale benchmark
does not fail loudly; it reports numbers. This test is the loud failure.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TASKS_PATH = ROOT / "benchmark" / "code-parity-v2.json"
# `scripts/retrieval.py:3107 (def _fused_candidates)` and the bare `:3113` that
# follows it in the same citation, which continues the previous path.
ANCHOR = re.compile(
    r"(?:(?P<path>[\w/.\-]+\.py))?"
    r"(?::(?P<line>\d+))?"
    r"(?:\s*\((?P<note>[^)]*)\))?"
)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _tasks() -> list[dict]:
    return json.loads(TASKS_PATH.read_text(encoding="utf-8"))["tasks"]


def _task_ids() -> list[str]:
    return [task["id"] for task in _tasks()]


def _task(task_id: str) -> dict:
    return next(task for task in _tasks() if task["id"] == task_id)


def _file_lines(path: str) -> list[str]:
    return (ROOT / path).read_text(encoding="utf-8").splitlines()


def _expected_name(note: str) -> str | None:
    """The identifier a citation's parenthesis claims for its line, if any."""
    match = IDENTIFIER.search(note.removeprefix("def "))
    if match is None:
        return None
    return match.group(0)


def _carried_path(match: re.Match[str], current: str) -> str:
    """A bare `:1304` continues the path its own citation named before it."""
    return match.group("path") or current


def _anchor_line(match: re.Match[str]) -> int:
    """0 for an anchor that names a definition instead of a line."""
    line = match.group("line")
    return int(line) if line else 0


def _anchor_fact(match: re.Match[str], path: str) -> tuple[str, int, str]:
    return (path, _anchor_line(match), match.group("note") or "")


def _is_a_definition_anchor(match: re.Match[str]) -> bool:
    """`path (def name)`: the one anchor shape allowed to carry no line."""
    return (match.group("note") or "").startswith("def ")


def _is_an_anchor(match: re.Match[str]) -> bool:
    """A line, or a definition named instead of one.

    Prose in a citation mentions paths freely — "grep -rn x scripts/ (on this
    date)" — so a match with neither a line nor a `def` note is not an anchor.
    """
    if match.group("line"):
        return True
    return _is_a_definition_anchor(match)


def _anchor_facts(citation: str) -> list[tuple[str, int, str]]:
    """Every (path, line, note) a citation names, carrying the path forward.

    A line number is optional. A definition's line moves whenever anything above
    it is edited, and on 2026-09-12 and again on 2026-09-13 that alone turned the
    suite red while the gold was still true — so a citation may name the file and
    the definition and leave the line to be resolved here. See
    `docs/research/2026-09-13-what-the-stands-must-show-after-the-verdict-cache.md`.
    """
    facts: list[tuple[str, int, str]] = []
    current = ""
    for match in ANCHOR.finditer(citation):
        if not _is_an_anchor(match):
            continue
        current = _carried_path(match, current)
        facts.append(_anchor_fact(match, current))
    return [fact for fact in facts if fact[0]]


def _resolved_line(path: str, line: int) -> str:
    lines = _file_lines(path)
    assert 1 <= line <= len(lines), f"{path}:{line} is past the end of the file"
    return lines[line - 1]


def _defining_lines(path: str, name: str) -> list[int]:
    """Every line of `path` that defines `name`, 1-based."""
    pattern = re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(name)}\b")
    return [
        number
        for number, text in enumerate(_file_lines(path), start=1)
        if pattern.match(text)
    ]


def _assert_definition_resolves(path: str, name: str) -> None:
    if _defining_lines(path, name):
        return
    matches = [text for text in _file_lines(path) if name in text]
    assert matches, f"{path} no longer names {name} at all"


def _assert_named_line_holds(fact: tuple[str, int, str], name: str) -> None:
    path, line, _note = fact
    text = _resolved_line(path, line)
    assert name in text, f"{path}:{line} does not name {name}: {text.strip()!r}"


def _assert_anchor_holds(fact: tuple[str, int, str]) -> None:
    path, line, note = fact
    if not (ROOT / path).is_file():
        pytest.fail(f"the gold cites {path}, which does not exist")
    name = _expected_name(note)
    if line == 0:
        _assert_definition_resolves(path, name or "")
        return
    if name is None:
        _resolved_line(path, line)
        return
    _assert_named_line_holds(fact, name)


def _terms_of(entry: object) -> list[str]:
    """A must entry is either one term or a tuple of terms that must co-occur."""
    if isinstance(entry, str):
        return [entry]
    return list(entry)


def _flat_terms(must: list) -> list[str]:
    return [term for entry in must for term in _terms_of(entry)]


def _numeric_terms(must: list) -> list[str]:
    return [term for term in _flat_terms(must) if term.isdigit()]


def _cited_lines(citations: list[str]) -> set[str]:
    facts = [fact for citation in citations for fact in _anchor_facts(citation)]
    return {str(line) for _, line, _ in facts if line}


def _unbacked_terms(gold: dict) -> list[str]:
    """Graded line numbers that no citation of the same task names."""
    cited = _cited_lines(gold["citations"])
    return [term for term in _numeric_terms(gold["must"]) if term not in cited]


@pytest.mark.parametrize("task_id", _task_ids())
def test_every_gold_citation_resolves_in_the_tree(task_id: str) -> None:
    facts = [
        fact
        for citation in _task(task_id)["gold"]["citations"]
        for fact in _anchor_facts(citation)
    ]
    for fact in facts:
        _assert_anchor_holds(fact)


@pytest.mark.parametrize("task_id", _task_ids())
def test_every_graded_line_number_is_one_the_citations_name(task_id: str) -> None:
    gold = _task(task_id)["gold"]
    unbacked = _unbacked_terms(gold)
    assert not unbacked, f"{task_id} grades on line numbers no citation names: {unbacked}"


def test_a_retired_task_states_why_it_left():
    document = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    retired = document.get("retired", [])
    stated = [entry for entry in retired if entry.get("reason") and entry.get("retired_on")]
    assert len(stated) == len(retired)


def test_a_definition_anchor_needs_no_line_number():
    """The name is the fact; the line is resolved here, so edits cannot rot it."""
    facts = _anchor_facts("scripts/search_memory.py (def _legacy_vector_source_membership)")

    assert facts == [("scripts/search_memory.py", 0, "def _legacy_vector_source_membership")]
    _assert_anchor_holds(facts[0])


def test_a_definition_that_left_the_file_still_fails():
    with pytest.raises(AssertionError):
        _assert_anchor_holds(("scripts/search_memory.py", 0, "def _gone_from_this_tree"))


def test_a_numbered_anchor_is_still_checked_against_its_line():
    with pytest.raises(AssertionError):
        _assert_anchor_holds(("scripts/search_memory.py", 1, "def fuse_rrf"))
