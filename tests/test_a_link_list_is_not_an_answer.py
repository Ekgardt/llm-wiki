"""A chunk that is mostly links ranks behind prose (audit 2026-09-26 C-10).

docs/research/2026-09-26-a-link-list-is-not-an-answer.md
"""
from __future__ import annotations

import pytest

RELATED = (
    "## Related\n"
    "- [[knowledge/notes/secret-shape-not-secret-name-decision]] — what the redactor may treat as a secret.\n"
    "- [[solo-operator-superset-product-decision]]\n"
    "- [[v4-reliability-contracts-decision]]\n"
    "- [[derived-evidence-generation-decision]]\n"
)
# Measured on this vault: link density 0.31, under the boilerpipe bound.
ANNOTATED = (
    "## Related\n"
    "- [[knowledge/notes/secret-shape-not-secret-name-decision]] — what the redactor may treat\n"
    "  as a secret: the value decides, not the name beside it.\n"
    "- [[knowledge/notes/self-resolving-health-findings-decision]] — the same principle applied\n"
    "  to health: a rule that can never pass is not a safety rule.\n"
)
PROSE = (
    "## Decision\n"
    "The redactor treats a value as a secret by its shape, not by the name beside it; "
    "see [[secret-shape-not-secret-name-decision]] for the measured misses.\n"
)


@pytest.mark.parametrize(
    ("content", "navigation"),
    [(RELATED, True), (ANNOTATED, True), ("## Related\n", True), (PROSE, False),
     ("- [[a]] is one option.\nThe other is prose.\n", False), (None, False), ("", False)],
)
def test_substance_weight_reads_link_density(content, navigation) -> None:
    from provenance import NAVIGATION_WEIGHT, substance_weight

    assert (substance_weight(content) == NAVIGATION_WEIGHT) is navigation


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


def test_a_chunk_is_judged_once_however_often_it_is_weighed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import provenance

    judged: list[str] = []
    real = provenance._navigation

    def recorded(text: str) -> bool:
        judged.append(text)
        return bool(real(text))

    monkeypatch.setattr(provenance, "_navigation", recorded)
    monkeypatch.setattr(provenance, "_NAVIGATION_VERDICTS", {})
    text = RELATED + "\n<!-- judged once -->"

    weights = {provenance.substance_weight(text) for _ in range(3)}

    assert weights == {provenance.NAVIGATION_WEIGHT}
    assert judged == [text]


def test_the_oldest_verdict_makes_room_past_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    import provenance

    monkeypatch.setattr(provenance, "_NAVIGATION_VERDICTS", {})
    monkeypatch.setattr(provenance, "NAVIGATION_VERDICTS_KEPT", 2)

    for text in ("first prose line", "second prose line", "third prose line"):
        provenance.substance_weight(text)

    assert len(provenance._NAVIGATION_VERDICTS) == 2
