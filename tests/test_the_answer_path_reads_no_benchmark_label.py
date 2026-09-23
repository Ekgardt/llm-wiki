"""The answer path reads the question and the store, never a benchmark label.

A trigger that reads `question_type`, the `_abs` suffix or a question id is
fitting to the benchmark, not a mechanism of the product: the open code of
several stands hands the answering model the label (RESEARCH-B, M6), and the
plan's first rule forbids it here. This guard walks every module of `scripts/`
that `query_memory` imports, directly or through another module, and fails
when any of them so much as names one of those labels. Rule 1 of
`docs/research/2026-09-22-a-class-level-plan-to-pass-the-field.md`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ENTRY = "query_memory"
# Whole words, so `_abs` catches `endswith("_abs")` and an `is_abs` flag alike.
FORBIDDEN = (
    re.compile(r"\bquestion_type\b"),
    re.compile(r"\bquestion_id\b"),
    re.compile(r"_abs\b"),
    re.compile(r"\bis_abstention\b"),
)


def _local_imports(tree: ast.AST) -> set[str]:
    """The `scripts/` modules a module imports, by either import form."""
    names: set[str] = set()
    for node in ast.walk(tree):
        names |= _imported_names(node)
    return {name for name in names if (SCRIPTS / f"{name}.py").is_file()}


def _imported_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom) and node.module:
        return {node.module.split(".")[0]}
    return set()


def answer_path_modules(entry: str = ENTRY) -> list[str]:
    """Every module reachable from the entry through `scripts/` imports."""
    seen: list[str] = []
    pending = [entry]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.append(name)
        pending.extend(_local_imports(ast.parse((SCRIPTS / f"{name}.py").read_text(encoding="utf-8"))))
    return sorted(seen)


def offending_labels(source: str) -> list[str]:
    """The benchmark labels a source text names, in a stable order."""
    return [pattern.pattern for pattern in FORBIDDEN if pattern.search(source)]


def test_the_guard_sees_a_planted_label() -> None:
    """A guard that cannot fail is no guard."""
    planted = 'kind = question["question_type"]\nif name.endswith("_abs"): pass\n'

    assert offending_labels(planted) == [r"\bquestion_type\b", r"_abs\b"]
    assert offending_labels("profile = analyze_query(question)") == []


def test_the_answer_path_reaches_its_own_modules() -> None:
    modules = answer_path_modules()

    assert {"query_memory", "retrieval", "evidence_sufficiency", "temporal_anchor"} <= set(modules)


def test_no_module_on_the_answer_path_names_a_benchmark_label() -> None:
    found = {
        name: offending_labels((SCRIPTS / f"{name}.py").read_text(encoding="utf-8"))
        for name in answer_path_modules()
    }

    assert {name: labels for name, labels in found.items() if labels} == {}
