"""A build that names code roots collects no knowledge pages; a memory build still does.

The vault's code generation of 2026-09-15 carried all 333 private pages although its code
roots exclude `knowledge`. Research:
`docs/research/2026-09-16-the-code-index-leaves-the-knowledge-alone.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import corpus_snapshot  # noqa: E402

PAGE = (
    "---\ntype: concept\nproject: tests\nsource_authority: user\nconfidence: high\n"
    "status: active\nvalid_from: 2026-01-01\nvalid_to: 2027-01-01\nlanguage: en\n---\n"
    "# A private page\nwhat the owner knows\n"
)


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    (root / "knowledge/projects").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "knowledge/notes/page.md").write_text(PAGE, encoding="utf-8")
    (root / "scripts/alpha.py").write_text("def helper(value):\n    return value\n", encoding="utf-8")
    return root


def _paths(snapshot) -> set[str]:
    return {source.record.relative_path for source in snapshot.sources}


def test_a_code_build_collects_its_roots_only(tmp_path) -> None:
    root = _vault(tmp_path)

    snapshot = corpus_snapshot.collect_corpus(root, code_roots=("scripts",))

    assert _paths(snapshot) == {"scripts/alpha.py"}


def test_a_memory_build_still_collects_the_pages(tmp_path) -> None:
    root = _vault(tmp_path)

    snapshot = corpus_snapshot.collect_corpus(root)

    assert _paths(snapshot) == {"knowledge/notes/page.md"}
