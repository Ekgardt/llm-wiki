"""A code generation is built without vectors; a memory generation still carries them.

Nothing opens a code generation's vectors — the dense leg reads the active generation, and
a code generation is never activated — yet they took 23 of the 27 minutes of the vault's
build. Research: `docs/research/2026-09-16-a-code-generation-needs-no-vectors.md`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_repository_index import ALPHA, _repository, vault  # noqa: E402,F401


def _manifest(state: Path, generation_id: str) -> dict:
    path = state / "cache/evidence-graph/generations" / generation_id / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_code_generation_says_its_vectors_are_absent(vault) -> None:  # noqa: F811 - the fixture
    import repository_index

    root, state = vault
    _repository(root, {"scripts/alpha.py": ALPHA})

    receipt = repository_index.index_repository(root, roots=["scripts"], state_root=state)

    directory = state / "cache/evidence-graph/generations" / receipt["generation_id"]
    manifest = _manifest(state, receipt["generation_id"])
    assert (manifest["vector_state"], (directory / "vectors.npy").exists()) == ("absent", False)


def test_the_builder_keeps_the_corpus_for_a_memory_generation() -> None:
    import evidence_graph_builder

    snapshot = object()

    kept = evidence_graph_builder._vector_snapshot(snapshot, {"code_roots": []})
    dropped = evidence_graph_builder._vector_snapshot(snapshot, {"code_roots": ["scripts"]})

    assert (kept, dropped) == (snapshot, None)
