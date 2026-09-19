"""A file asked for by its own name is the first result, whatever kind of file it is.

Third audit, 2026-09-17: the promotion of an exact filename ran before page
diversity and the lane score, which moved a named raw or daily file back
behind the compiled pages. See
`docs/research/2026-09-17-a-named-file-stays-first.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import retrieval  # noqa: E402

NAMED = "knowledge/raw/2026-09-01.md"


def _hit(candidate_id: str, path: str, page_type: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "parent_id": path,
        "relative_path": path,
        "heading_path": (),
        "source_sha256": "a" * 64,
        "byte_start": 0,
        "byte_end": 10,
        "score": 1.0,
        "type": page_type,
    }


def _retrieved(query: str):
    hits = [
        _hit("c-page", "knowledge/notes/release-plan.md", "decision"),
        _hit("c-other", "knowledge/raw/2026-08-30.md", "raw-source"),
        _hit("c-named", NAMED, "raw-source"),
    ]
    return retrieval.retrieve(
        query,
        requested_profile="BASE",
        limit=3,
        lexical_backend=lambda **_kwargs: hits,
        rerank_enabled=False,
        corpus_generation="gen-named-file",
    )


def test_a_raw_file_named_by_its_date_leads_the_compiled_pages() -> None:
    result = _retrieved("2026-09-01")

    assert result.candidates[0].relative_path == NAMED
    assert retrieval._reported_mode(result, "2026-09-01") == "EXACT"


def test_a_question_that_names_no_file_keeps_the_compiled_page_first() -> None:
    result = _retrieved("what is the release plan")

    assert result.candidates[0].relative_path == "knowledge/notes/release-plan.md"
