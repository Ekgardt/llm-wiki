"""Load and sample the LongMemEval dataset (MEM-10).

The dataset is the `longmemeval_s` variant of `xiaowu0162/longmemeval`
(500 questions, ~50 haystack chat sessions each, MIT license). It is cached
under `cache/benchmarks/longmemeval/` — the disposable cache tier — and is
NEVER generated: when the file is absent this module refuses with the exact
download instruction instead of inventing a synthetic stand-in.

Note recorded for honesty: Hugging Face marks this dataset as deprecated in
favour of `longmemeval-cleaned`; the original is still what the published
Mem0 / Zep claims were measured on, so the first number is taken here.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

DATASET_DIR = Path(__file__).resolve().parent.parent / "cache" / "benchmarks" / "longmemeval"
DATASET_FILE = DATASET_DIR / "longmemeval_s.json"
DATASET_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_s"
)
EXPECTED_QUESTIONS = 500
REQUIRED_KEYS = frozenset(
    {
        "question_id",
        "question_type",
        "question",
        "answer",
        "question_date",
        "haystack_dates",
        "haystack_session_ids",
        "haystack_sessions",
    }
)


class DatasetUnavailable(RuntimeError):
    """The real dataset is not on disk; a synthetic stand-in is forbidden."""


def load_dataset(path: Path = DATASET_FILE) -> list[dict]:
    if not path.exists():
        raise DatasetUnavailable(
            f"LongMemEval is not cached at {path}. Download the real file "
            f"(278 MB) from {DATASET_URL} and save it there. This harness "
            "never generates a synthetic stand-in."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    require_dataset_shape(data)
    return data


def require_dataset_shape(data: object) -> None:
    if not isinstance(data, list) or not data:
        raise DatasetUnavailable("dataset must be a non-empty JSON array of questions")
    missing = REQUIRED_KEYS - set(data[0])
    if missing:
        raise DatasetUnavailable(f"dataset questions lack required keys: {sorted(missing)}")


def is_abstention(question: dict) -> bool:
    """LongMemEval marks abstention questions by a `_abs` question-id suffix."""
    return str(question["question_id"]).endswith("_abs")


def category_of(question: dict) -> str:
    """Reporting category: the paper scores abstention as its own row."""
    if is_abstention(question):
        return "abstention"
    return str(question["question_type"])


def _stratum_of(question: dict) -> tuple[str, bool]:
    return (str(question["question_type"]), is_abstention(question))


def _grouped(data: list[dict]) -> dict[tuple[str, bool], list[dict]]:
    groups: dict[tuple[str, bool], list[dict]] = {}
    for question in data:
        groups.setdefault(_stratum_of(question), []).append(question)
    return groups


def _largest_remainder(quotas: dict[tuple, float], size: int) -> dict[tuple, int]:
    """Integer allocation that keeps every stratum proportional to its share."""
    base = {key: int(quota) for key, quota in quotas.items()}
    short = size - sum(base.values())
    by_fraction = sorted(quotas, key=lambda key: (quotas[key] - int(quotas[key]), key), reverse=True)
    for key in by_fraction[:short]:
        base[key] += 1
    return base


def stratified_sample(data: list[dict], size: int, seed: int) -> list[dict]:
    """A deterministic proportional sample across (question_type, abstention).

    Deterministic for a (data, size, seed) triple: strata are visited in
    sorted order and rows are sorted by question id before the draw, so the
    same command line names the same questions on every machine.
    """
    if size >= len(data):
        return list(data)
    groups = _grouped(data)
    quotas = {key: size * len(rows) / len(data) for key, rows in groups.items()}
    allocation = _largest_remainder(quotas, size)
    rng = random.Random(seed)
    picked: list[dict] = []
    for key in sorted(groups):
        rows = sorted(groups[key], key=lambda question: str(question["question_id"]))
        picked.extend(rng.sample(rows, min(allocation[key], len(rows))))
    return picked
