"""Shared contract of the retrieval-v2 benchmark: repository paths, frozen identities, limits,
report fields, lexical configurations and the value types every part exchanges."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Explicit sentinel: Task 8 adapters have no network implementation or model download path.
urlopen = None


DEFAULT_CORPUS = Path(__file__).with_name("retrieval-v2.json")
DEFAULT_SCHEMA = Path(__file__).with_name("retrieval-v2.schema.json")
DEFAULT_MATRIX = Path(__file__).with_name("model-matrix-v1.json")

RUNNER_PATH = Path(__file__).resolve().with_name("run_retrieval_v2.py")

# The frozen corpora this runner accepts. The cross-lingual corpus (2026-09-10)
# carries the same 14 documents and 24 added cross-language queries; it has its
# own matrix copy because the contract pins the corpus bytes.
FROZEN_CORPUS_IDS = frozenset(
    {"public-synthetic-retrieval-v2", "public-synthetic-retrieval-v2-crosslingual"}
)
ADAPTER_KIND = "deterministic-fake"
MODEL_MATRIX_ADAPTER_KIND = "model-matrix"
SELECTION_AGGREGATION_ADAPTER_KIND = "selection-aggregation"
AUTHORITATIVE_SELECTION_ADAPTER_KIND = "authoritative-selection"
MAX_CANDIDATES = 50
MAX_CORPUS_BYTES = 2 * 1024 * 1024
MAX_SCHEMA_BYTES = 64 * 1024
DEFAULT_LEXICAL_DEADLINE_SECONDS = 30.0
MAX_SEGMENTATION_INPUT_CHARS = 100_000
MAX_SEGMENTATION_TOKENS = 10_000
THRESHOLDS = {
    "parent_recall_at_10": 0.95,
    "all_required_evidence_recall_at_20": 0.85,
    "ndcg_at_10": 0.80,
    "mrr_at_10": 0.85,
    "max_language_gate_gap": 0.03,
    "no_answer_false_answer_rate": 0.03,
}
EFFECTIVENESS_FIELDS = (
    "evidence_recall_at_10",
    "evidence_recall_at_20",
    "evidence_recall_at_50",
    "all_required_evidence_recall_at_20",
    "parent_recall_at_10",
    "ndcg_at_10",
    "mrr_at_10",
    "no_answer_false_answer_rate",
)
REPORT_FIELDS = {
    "schema_version",
    "corpus_id",
    "adapter_kind",
    "quality_claim",
    "methodology",
    "overall",
    "slices",
    "macro_average",
    "thresholds",
    "gates",
    "measurements",
    "traces",
    "model_id",
    "variant_id",
    "revision",
    "matrix_sha256",
    "corpus_sha256",
    "acquisition_mode",
    "vector_backend",
    "reranker",
    "release_evidence",
    "requested_mode",
    "effective_mode",
    "fallback_reason",
    "benchmark_contract_sha256",
    "benchmark_runner_sha256",
    "candidate",
}
SELECTION_ARTIFACT_FIELDS = {
    "baseline",
    "benchmark_contract_sha256",
    "benchmark_runner_sha256",
    "candidate_reports",
    "corpus_sha256",
    "gates",
    "matrix_policy_sha256",
    "matrix_sha256",
    "measured_at",
    "measurements",
    "pareto",
    "quality_claim",
    "release_evidence",
    "schema_version",
    "selected",
}
LEXICAL_CONFIGURATIONS = {
    "L0": {
        "id": "L0",
        "indexes": {"unicode": "unicode61 remove_diacritics 2"},
        "routing": {"strategy": "all-indexes", "candidate_universe": "all-eligible"},
        "fallback": None,
        "segmentation": None,
        "fusion": None,
    },
    "L1": {
        "id": "L1",
        "indexes": {
            "unicode": "unicode61 remove_diacritics 2",
            "english_porter": "porter unicode61 remove_diacritics 2",
        },
        "routing": {"strategy": "all-indexes", "candidate_universe": "all-eligible"},
        "fallback": None,
        "segmentation": None,
        "fusion": {"method": "reciprocal-rank-fusion", "k": 60},
    },
    "L2": {
        "id": "L2",
        "indexes": {
            "unicode": "unicode61 remove_diacritics 2",
            "chinese_trigram": "trigram",
        },
        "routing": {"strategy": "all-indexes", "candidate_universe": "all-eligible"},
        "fallback": "unicode",
        "segmentation": None,
        "fusion": {"method": "reciprocal-rank-fusion", "k": 60},
    },
    "L3": {
        "id": "L3",
        "indexes": {
            "unicode": "unicode61 remove_diacritics 2",
            "chinese_jieba": "unicode61 remove_diacritics 2",
        },
        "routing": {"strategy": "all-indexes", "candidate_universe": "all-eligible"},
        "fallback": None,
        "segmentation": {"dependency": "jieba", "version": "0.42.1", "HMM": False},
        "fusion": {"method": "reciprocal-rank-fusion", "k": 60},
    },
    "L4": {
        "id": "L4",
        "indexes": {
            "unicode": "unicode61 remove_diacritics 2",
            "english_porter": "porter unicode61 remove_diacritics 2",
            "chinese_trigram": "trigram",
            "chinese_jieba": "unicode61 remove_diacritics 2",
        },
        "routing": {"strategy": "all-indexes", "candidate_universe": "all-eligible"},
        "fallback": "unicode",
        "segmentation": {"dependency": "jieba", "version": "0.42.1", "HMM": False},
        "fusion": {"method": "reciprocal-rank-fusion", "k": 60},
    },
}


@dataclass(frozen=True)
class QueryScope:
    projects: tuple[str, ...]
    temporal_mode: str
    as_of: str | None


@dataclass(frozen=True)
class Candidate:
    evidence_id: str
    parent_id: str
    relative_path: str
    language: str
    project: str
    status: str
    valid_from: str
    valid_to: str | None
    heading_path: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    score: float

    @property
    def evidence_id(self) -> str:
        return self.candidate.evidence_id

    @property
    def parent_id(self) -> str:
        return self.candidate.parent_id

    @property
    def text(self) -> str:
        return self.candidate.text


@dataclass(frozen=True)
class ModelSelection:
    matrix: dict
    embedding: dict
    variant: dict
    reranker: dict | None
    reranker_variant: dict | None
    matrix_sha256: str
    corpus_sha256: str


@dataclass(frozen=True)
class WorkerPayload:
    report: dict
    canonical_bytes: bytes
