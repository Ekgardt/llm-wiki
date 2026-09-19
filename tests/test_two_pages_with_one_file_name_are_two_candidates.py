"""Two projects' `state.md` pages are two results, not one with a doubled score.

Third audit, 2026-09-17: the legacy search path named a candidate by its file
stem, and fusion adds ranks per name, so every `knowledge/projects/<slug>/state.md`
collapsed into one candidate `state`. See
`docs/research/2026-09-17-two-pages-with-one-file-name-are-two-candidates.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import retrieval  # noqa: E402
import search_memory  # noqa: E402

PAGES = (
    "knowledge/projects/alpha/state.md",
    "knowledge/projects/beta/state.md",
    "knowledge/notes/rollout.md",
)


def _vault(root: Path) -> list[Path]:
    written = []
    for relative in PAGES:
        page = root / relative
        page.parent.mkdir(parents=True)
        page.write_text(f"# {relative}\n\nThe rollout checkpoint is pending review.\n", encoding="utf-8")
        written.append(page)
    return written


def _legacy_hits(pages: list[Path]) -> list[dict]:
    return search_memory._direct_markdown_hits(
        "rollout checkpoint", pages, limit=5, project=None, since=None, as_of=None,
        deadline=None, cancelled=None,
    )


def test_both_state_pages_survive_fusion(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    hits = _legacy_hits(_vault(tmp_path))

    result = retrieval.retrieve(
        "rollout checkpoint",
        requested_profile="BASE",
        limit=5,
        lexical_backend=lambda **_kwargs: hits,
        rerank_enabled=False,
        corpus_generation="legacy",
    )

    assert sorted(item.relative_path for item in result.candidates) == sorted(PAGES)


def test_a_flat_note_keeps_its_slug_and_any_other_file_is_named_by_its_path() -> None:
    names = [search_memory.legacy_candidate_id(path) for path in (*PAGES, "")]

    assert names == ["knowledge/projects/alpha/state", "knowledge/projects/beta/state", "rollout", ""]
