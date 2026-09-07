#!/usr/bin/env python3
"""RefusalBench, converted into the shape this stand already runs.

RefusalBench asks the one question LongMemEval cannot: given a context that is
ambiguous, contradictory, missing the answer, or built on a false premise, does
the system refuse — and does it still answer the ones it can? LongMemEval
scores our silence as a miss, so every number we have understates the thing
this product is built to do. This is the stand that scores it directly.

Two sets, both released with the paper (arXiv:2510.10390):

- **NQ**, 1 600 rows, one passage each: 536 that must be answered and 1 064
  that must be refused, across six kinds of flaw at three intensities.
- **GaRAGe**, 1 506 rows, ten passages each with the signal and the noise
  labelled: 450 answerable, 1 056 refusable.

As with LoCoMo there is no second harness. Each row becomes a question in the
LongMemEval shape — the passages become the sessions of a one-conversation
haystack — so `run_longmemeval.py --dataset` runs it with the same sampler,
worker and scorer, and a row that must be refused takes the `_abs` suffix that
`longmemeval_data.is_abstention` already understands.

## What this conversion changes about the benchmark, stated plainly

RefusalBench hands the generator its context. We put that context in a vault
and let retrieval find it, because our generator only reads spans our own
retrieval produced and there is no honest way around that. So a refusal here
can have two causes: the contract refused the flawed context, which is what
the benchmark measures, or retrieval never surfaced the passage, which is not.
The harness records `answer_retrieved` per row, so the two are separable and
must be reported separately. With one conversation in the haystack the second
cause should be rare, and if it is not, that is itself worth knowing.

Source: Muhamed et al., "RefusalBench: Generative Evaluation of Selective
Refusal in Grounded Language Models". NQ is Apache-2.0; GaRAGe is CC BY-NC 4.0.
"""

from __future__ import annotations

import json
from pathlib import Path

DATASET_DIR = (
    Path(__file__).resolve().parent.parent / "cache" / "benchmarks" / "refusalbench"
)
NQ_FILE = DATASET_DIR / "refusalbench-nq.jsonl"
GARAGE_FILE = DATASET_DIR / "refusalbench-garage.jsonl"
CONVERTED_NQ = DATASET_DIR / "refusalbench-nq-questions.json"
CONVERTED_GARAGE = DATASET_DIR / "refusalbench-garage-questions.json"
NQ_URL = (
    "https://huggingface.co/datasets/aashiqmuhamed/RefusalBench-NQ/resolve/main/"
    "refusalbench-nq.jsonl"
)
GARAGE_URL = (
    "https://huggingface.co/datasets/aashiqmuhamed/RefusalBench-GaRAGe/resolve/main/"
    "refusalbench-garage.jsonl"
)
ANSWERABLE = "ANSWER_CORRECTLY"
ASKED_ON = "2026/01/15 (Thu) 12:00"
SPOKEN_ON = "2026/01/14 (Wed) 09:00"


class DatasetUnavailable(RuntimeError):
    """The real dataset is not on disk; a synthetic stand-in is forbidden."""


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        raise DatasetUnavailable(
            f"RefusalBench is not cached at {path}. Download the real file "
            f"from {NQ_URL} or {GARAGE_URL} and save it there. This harness "
            "never generates a synthetic stand-in."
        )
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise DatasetUnavailable(f"{path} holds no rows")
    return rows


def _must_refuse(row: dict) -> bool:
    return str(row.get("expected_rag_behavior")) != ANSWERABLE


def _answer_of(row: dict) -> str:
    """The answer a correct system gives, or nothing when silence is correct."""
    if _must_refuse(row):
        return ""
    answers = row.get("original_answers")
    if isinstance(answers, list) and answers:
        return str(answers[0])
    return str(row.get("reference_answer") or "")


def _question_of(row: dict) -> str:
    return str(row.get("perturbed_query") or row.get("query") or "")


_CONTEXT_KEYS = ("perturbed_context", "original_context")


def _first_text(record: dict, keys: tuple[str, ...]) -> str:
    parts = (str(record.get(key) or "").strip() for key in keys)
    return next((part for part in parts if part), "")


def _cite_keys(passage: dict) -> tuple[str, ...]:
    """GaRAGe numbers each passage's text field `cite_<n>`, n varying per row."""
    return tuple(key for key in passage if key.startswith("cite_"))


def _passage_text(passage: object) -> str:
    """One grounding passage, whichever of the two shapes it arrives in."""
    if isinstance(passage, str):
        return passage.strip()
    if not isinstance(passage, dict):
        return ""
    return _first_text(passage, _cite_keys(passage) or ("text", "content"))


def _grounding_passages(grounding: list) -> list[str]:
    return [text for text in map(_passage_text, grounding) if text]


def _passages(row: dict) -> list[str]:
    grounding = row.get("grounding")
    if isinstance(grounding, list):
        return _grounding_passages(grounding)
    context = _first_text(row, _CONTEXT_KEYS)
    return [context] if context else []


def _sessions(row: dict) -> list[list[dict]]:
    """Each passage is a session, so retrieval ranks them as it ranks anything."""
    return [
        [
            {"role": "user", "content": "Here is a source I want to remember."},
            {"role": "assistant", "content": text},
        ]
        for text in _passages(row)
    ]


def _question_id(row: dict) -> str:
    identifier = str(row.get("id") or "")
    suffix = "_abs" if _must_refuse(row) else ""
    return f"{identifier}{suffix}"


def _question_type(row: dict) -> str:
    """The flaw the row carries, which is what a per-category report needs."""
    return str(row.get("perturbation_class") or "unknown")


def _converted(row: dict, sessions: list[list[dict]]) -> dict:
    return {
        "question_id": _question_id(row),
        "question_type": _question_type(row),
        "question": _question_of(row),
        "answer": _answer_of(row),
        "question_date": ASKED_ON,
        "haystack_dates": [SPOKEN_ON] * len(sessions),
        "haystack_session_ids": [
            f"{row.get('id')}-p{index}" for index in range(len(sessions))
        ],
        "haystack_sessions": sessions,
        "answer_session_ids": [],
        "refusal_expected_behavior": str(row.get("expected_rag_behavior")),
        "refusal_intensity": str(row.get("intensity") or ""),
    }


def _is_usable(row: dict, sessions: list[list[dict]]) -> bool:
    return bool(sessions) and bool(_question_of(row))


def converted_questions(rows: list[dict]) -> list[dict]:
    pairs = ((row, _sessions(row)) for row in rows)
    return [
        _converted(row, sessions) for row, sessions in pairs if _is_usable(row, sessions)
    ]


def convert(source: Path, target: Path) -> Path:
    questions = converted_questions(load_rows(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(questions, ensure_ascii=False), encoding="utf-8")
    return target


def _report(name: str, target: Path) -> None:
    questions = json.loads(target.read_text(encoding="utf-8"))
    refusable = sum(1 for item in questions if item["question_id"].endswith("_abs"))
    print(f"{name}: {len(questions)} question(s) → {target}")
    print(f"  must refuse: {refusable}   must answer: {len(questions) - refusable}")


def main() -> int:
    for name, source, target in (
        ("RefusalBench-NQ", NQ_FILE, CONVERTED_NQ),
        ("RefusalBench-GaRAGe", GARAGE_FILE, CONVERTED_GARAGE),
    ):
        _report(name, convert(source, target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
