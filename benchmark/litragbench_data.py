#!/usr/bin/env python3
"""LIT-RAGBench, converted into the shape this stand already runs.

114 human-written questions over five capabilities — Integration, Reasoning,
Logic, Table and Abstention — each with the passages that hold the answer and
the passages that do not. Sixty of the 114 are abstention: the context is cut
off, self-contradictory, or holds nothing but decoys, and the reference answer
says so in words. It is small, and it is more than half the thing LongMemEval
punishes us for, which is why it is worth its size.

As with LoCoMo and RefusalBench there is no second harness: each row becomes a
question in the LongMemEval shape, positive and negative chunks alike become
sessions of a one-conversation haystack, and an abstention row takes the
`_abs` suffix `longmemeval_data.is_abstention` already understands.

## What this conversion decides

- **The decoys go in too.** Dropping `negative_chunk_list` would hand our
  retrieval a corpus with only the answer in it, which is not the task. Both
  lists are ingested and retrieval has to tell them apart, which is closer to
  what the vault does every day than the benchmark's own setting is.
- **An abstention row's answer is emptied.** The dataset's reference answer
  for those rows is a sentence explaining that the context does not support an
  answer. Scored as a string that is not what abstention means here: the
  system is right if it says nothing, and the reference sentence is kept in
  `litrag_reference_answer` for reading, not for scoring.
- **The English edition.** The Japanese original and its machine-translated,
  human-curated English version both ship; we run English because every other
  stand here is English and a cross-language comparison would confound.

Source: Itai et al., "LIT-RAGBench: Benchmarking Generator Capabilities of
Large Language Models in Retrieval-Augmented Generation" (arXiv:2603.06198),
data at `neoai-inc/LIT-RAGBench`.
"""

from __future__ import annotations

import json
from pathlib import Path

DATASET_DIR = (
    Path(__file__).resolve().parent.parent / "cache" / "benchmarks" / "litragbench"
)
SOURCE_FILE = DATASET_DIR / "en.jsonl"
CONVERTED_FILE = DATASET_DIR / "litragbench-questions.json"
SOURCE_URL = (
    "https://huggingface.co/datasets/neoai-inc/LIT-RAGBench/resolve/main/en.jsonl"
)
EXPECTED_QUESTIONS = 114
ABSTENTION_PREFIX = "A_"
CAPABILITY = {
    "A": "abstention",
    "I": "integration",
    "L": "logic",
    "R": "reasoning",
    "T": "table",
}
ASKED_ON = "2026/01/15 (Thu) 12:00"
SPOKEN_ON = "2026/01/14 (Wed) 09:00"


class DatasetUnavailable(RuntimeError):
    """The real dataset is not on disk; a synthetic stand-in is forbidden."""


def load_rows(path: Path = SOURCE_FILE) -> list[dict]:
    if not path.exists():
        raise DatasetUnavailable(
            f"LIT-RAGBench is not cached at {path}. Download the real file "
            f"from {SOURCE_URL} and save it there. This harness never "
            "generates a synthetic stand-in."
        )
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise DatasetUnavailable(f"{path} holds no rows")
    return rows


def _code_of(row: dict) -> str:
    types = row.get("qa_type")
    if isinstance(types, list) and types:
        return str(types[0])
    return str(types or "")


def _must_refuse(row: dict) -> bool:
    return _code_of(row).startswith(ABSTENTION_PREFIX)


def _capability_of(row: dict) -> str:
    code = _code_of(row)
    return CAPABILITY.get(code[:1], "unknown")


def _titled(title: str, content: str) -> str:
    return "\n".join(part for part in (title, content) if part)


def _chunk_text(chunk: object) -> str:
    if isinstance(chunk, str):
        return chunk.strip()
    if not isinstance(chunk, dict):
        return ""
    return _titled(
        str(chunk.get("title") or "").strip(), str(chunk.get("content") or "").strip()
    )


def _chunks(row: dict) -> list[str]:
    """Both lists: retrieval has to tell the answer from the decoys."""
    listed = list(row.get("positive_chunk_list") or []) + list(
        row.get("negative_chunk_list") or []
    )
    return [text for text in map(_chunk_text, listed) if text]


def _sessions(row: dict) -> list[list[dict]]:
    return [
        [
            {"role": "user", "content": "Here is a source I want to remember."},
            {"role": "assistant", "content": text},
        ]
        for text in _chunks(row)
    ]


def _answer_of(row: dict) -> str:
    """An abstention row is right when nothing is said, not when a sentence is."""
    if _must_refuse(row):
        return ""
    return str(row.get("answer") or "")


def _question_id(row: dict, index: int) -> str:
    suffix = "_abs" if _must_refuse(row) else ""
    return f"LIT-{index:03d}-{_code_of(row)}{suffix}"


def _converted(row: dict, index: int, sessions: list[list[dict]]) -> dict:
    return {
        "question_id": _question_id(row, index),
        "question_type": _capability_of(row),
        "question": str(row.get("question") or ""),
        "answer": _answer_of(row),
        "question_date": ASKED_ON,
        "haystack_dates": [SPOKEN_ON] * len(sessions),
        "haystack_session_ids": [f"lit-{index:03d}-c{n}" for n in range(len(sessions))],
        "haystack_sessions": sessions,
        "answer_session_ids": [],
        "litrag_code": _code_of(row),
        "litrag_reference_answer": str(row.get("answer") or ""),
    }


def _is_usable(row: dict, sessions: list[list[dict]]) -> bool:
    return bool(sessions) and bool(str(row.get("question") or "").strip())


def converted_questions(rows: list[dict]) -> list[dict]:
    numbered = ((index, row, _sessions(row)) for index, row in enumerate(rows))
    return [
        _converted(row, index, sessions)
        for index, row, sessions in numbered
        if _is_usable(row, sessions)
    ]


def convert(source: Path = SOURCE_FILE, target: Path = CONVERTED_FILE) -> Path:
    questions = converted_questions(load_rows(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(questions, ensure_ascii=False), encoding="utf-8")
    return target


def main() -> int:
    target = convert()
    questions = json.loads(target.read_text(encoding="utf-8"))
    refusable = sum(1 for item in questions if item["question_id"].endswith("_abs"))
    print(f"wrote {len(questions)} question(s) to {target}")
    print(f"  must refuse: {refusable}   must answer: {len(questions) - refusable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
