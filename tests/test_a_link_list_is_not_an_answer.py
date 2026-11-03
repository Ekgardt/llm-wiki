"""A chunk that is mostly links ranks behind prose (audit 2026-09-26 C-10).

docs/research/2026-09-26-a-link-list-is-not-an-answer.md
"""
from __future__ import annotations

from tests.modules.provenance.fixtures import PROSE, RELATED


def _row(candidate_id: str, content: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "parent_id": f"{candidate_id}.md",
        "relative_path": f"knowledge/notes/{candidate_id}.md",
        "heading_path": (),
        "source_sha256": "a" * 64,
        "byte_start": 0,
        "byte_end": 10,
        "score": 1.0,
        "content": content,
    }


def test_fusion_ranks_prose_above_a_link_list_the_lexical_leg_put_first() -> None:
    import retrieval

    fused, meta = retrieval.fuse_rrf(
        lexical=[_row("links", RELATED), _row("prose", PROSE)], dense=None, graph=None
    )

    assert [item.candidate_id for item in fused] == ["prose", "links"]
    assert meta["links"]["substance_weight"] < meta["prose"]["substance_weight"]


def test_the_generation_search_weighs_a_link_list_down() -> None:
    import search_memory

    def row(content: str) -> dict[str, object]:
        fields = dict.fromkeys(
            ("authority", "confidence", "project", "status", "type", "valid_from", "valid_to",
             "heading_ancestry", "language", "source_id", "chunk_id", "source_sha256",
             "byte_start", "byte_end", "span_sha256")
        )
        return {**fields, "heading_ancestry": "[]", "rank": -5.0, "source_path": "knowledge/notes/x.md", "title": "x",
                "content": content, "chunk_order": 0}

    link_score = search_memory._generation_result(row(RELATED), "g")["score"]
    prose_score = search_memory._generation_result(row(PROSE), "g")["score"]

    assert link_score < prose_score
