"""The curated-pages-first prior applies to knowledge questions only.

Measured on this vault before the prior became conditional: a design spec under
`docs/` that declares `type: decision` ranked above the decision page in
`knowledge/notes/` it comments on, and a question sheet under `benchmark/`,
typed `code`, ranked above everything. Both are commentary when the question is
about what the vault decided — and neither is when the question is about code.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import provenance  # noqa: E402
import retrieval  # noqa: E402

KNOWLEDGE_PAGE = "knowledge/notes/dead-task-retirement-and-restore-decision.md"
DOCS_SPEC = "docs/superpowers/specs/2026-08-05-v4-reliability-repair-design.md"
BENCHMARK_SHEET = "benchmark/vault-application-v1.json"
SOURCE_FILE = "scripts/retrieval.py"


def _weight(page_type, path, *, question):
    intents = retrieval.analyze_query(question).intents
    return provenance.source_type_weight(
        page_type, path, curated_first=provenance.curated_pages_first(intents)
    )


def test_a_knowledge_question_reads_code_root_sources_as_commentary():
    question = "как вернуть в работу задачу, у которой кончились попытки"

    assert _weight("decision", KNOWLEDGE_PAGE, question=question) == (
        provenance.TYPE_WEIGHTS["decision"]
    )
    for path in (DOCS_SPEC, BENCHMARK_SHEET, SOURCE_FILE):
        assert _weight("decision", path, question=question) == (
            provenance.TYPE_WEIGHTS["doc"]
        )
        assert _weight("code", path, question=question) == provenance.TYPE_WEIGHTS["doc"]


def test_a_code_question_leaves_code_and_docs_exactly_where_they_were():
    """Today's weights, unchanged: the prior is off, so nothing is demoted."""
    for question in (
        "what does scripts/retrieval.py::_weigh_by_trust do",
        "who calls fuse_rrf",
        "покажи структуру проекта",
    ):
        assert _weight("code", SOURCE_FILE, question=question) == (
            provenance.DEFAULT_TYPE_WEIGHT
        )
        assert _weight("decision", DOCS_SPEC, question=question) == (
            provenance.TYPE_WEIGHTS["decision"]
        )
        assert _weight("decision", KNOWLEDGE_PAGE, question=question) == (
            provenance.TYPE_WEIGHTS["decision"]
        )


def test_the_prior_is_only_ever_turned_off_by_a_positive_reading():
    """An unreadable or absent intent list keeps the vault's own rule."""
    assert provenance.curated_pages_first(()) is True
    assert provenance.curated_pages_first(None) is True
    assert provenance.curated_pages_first(("question", "cross_language")) is True
    assert provenance.curated_pages_first(("exact_identifier",)) is False


def test_living_under_a_code_root_never_lifts_a_page():
    """Raw evidence stays at its own weight; the rule only ever demotes."""
    raw = provenance.source_type_weight(
        "raw-source", "docs/notes/transcript.md", curated_first=True
    )
    assert raw == provenance.TYPE_WEIGHTS["raw-source"]
    assert raw < provenance.TYPE_WEIGHTS["doc"]


_ROWS = (
    {
        "candidate_id": "one",
        "relative_path": BENCHMARK_SHEET,
        "type": "code",
        "bm25_rank": 1,
    },
    {
        "candidate_id": "two",
        "relative_path": KNOWLEDGE_PAGE,
        "type": "decision",
        "bm25_rank": 2,
    },
)


def _fused(intents):
    """(ordered paths, weight by path) for one fusion under these intents."""
    candidates, _meta = retrieval.fuse_rrf(
        lexical=list(_ROWS), dense=None, graph=None, intents=intents
    )
    paths = [item.relative_path for item in candidates]
    weights = {item.relative_path: item.type_weight for item in candidates}
    return paths, weights


def test_fusion_records_the_conditional_weight_for_every_later_stage():
    """The reranker reads what fusion recorded, so one decision reaches both."""
    paths, weights = _fused(("question",))

    assert paths[0] == KNOWLEDGE_PAGE
    assert weights[BENCHMARK_SHEET] == provenance.TYPE_WEIGHTS["doc"]


def test_fusion_for_a_code_question_records_todays_weights():
    _paths, weights = _fused(("exact_identifier",))

    assert weights[BENCHMARK_SHEET] == provenance.DEFAULT_TYPE_WEIGHT
    assert weights[KNOWLEDGE_PAGE] == provenance.TYPE_WEIGHTS["decision"]
