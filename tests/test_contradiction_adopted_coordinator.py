"""The benchmark must open the coordinator adoption chose, not the legacy path.

Reliability V3 adoption replaces `run/markdown-transactions.sqlite3` with a JSON
tombstone. Anything that constructs `MarkdownCoordinator(vault, state_root)`
itself opens that tombstone as SQLite and dies with `file is not a database`,
so the rule has to be asked for rather than re-implemented or bypassed.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import contradiction_pipeline

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "scripts/contradiction_pipeline.py"
CORPUS = ROOT / "benchmark/contradiction-v1.json"


def _direct_coordinator_calls(source: str) -> list[int]:
    tree = ast.parse(source)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "MarkdownCoordinator"
    ]


def test_no_call_site_constructs_the_pre_adoption_coordinator():
    lines = _direct_coordinator_calls(MODULE.read_text(encoding="utf-8"))
    assert lines == [], (
        "these lines construct the legacy coordinator directly and would open the "
        f"adoption tombstone as SQLite: {lines}"
    )


def test_the_benchmark_asks_adoption_for_every_coordinator_it_opens(monkeypatch):
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    corpus["cases"] = corpus["cases"][:2]
    asked: list[tuple[Path, Path]] = []
    real = contradiction_pipeline.active_or_legacy_coordinator

    def spy(vault: Path, state_root: Path):
        asked.append((vault, state_root))
        return real(vault, state_root)

    monkeypatch.setattr(contradiction_pipeline, "active_or_legacy_coordinator", spy)
    contradiction_pipeline.run_frozen_benchmark(corpus)

    # One for seeding the source and ledger pages, one for publishing the results.
    assert len(asked) == 2
    assert {vault for vault, _state_root in asked} == {asked[0][0]}
