#!/usr/bin/env python3
"""BEAM, converted into the shape this stand already runs.

BEAM (arXiv:2510.27246, ICLR 2026) builds long coherent conversations and asks
ten kinds of memory question about them. The 100K track is twenty
conversations of about half a million characters each, three batches of turns
apiece, with twenty probing questions per conversation — two for each ability.

Only the abilities whose questions carry a gold answer are converted, because
this harness scores a string and a judge, not a rubric:

- kept: abstention, contradiction_resolution, event_ordering,
  information_extraction, knowledge_update, multi_session_reasoning,
  temporal_reasoning — 140 of the 200 in the 100K track.
- dropped: instruction_following and preference_following, which ship
  `expected_compliance` and a rubric instead of an answer; and summarization,
  which ships an ideal summary to be graded against bullet points. Scoring
  those needs a rubric judge this stand does not have, and pretending a rubric
  is a string would report a number that means nothing.

Its abstention questions carry `why_unanswerable`, so they join the same
`_abs` split LoCoMo, RefusalBench and LIT-RAGBench feed.

## What this conversion decides

- **A batch is a session.** Each of the three chat batches has one
  `time_anchor` shared by its turns; that anchor dates the session.
- **The `->-> b,t` marker is stripped.** BEAM appends a batch/turn coordinate
  to every message; it is bookkeeping, not something anyone said.
- **Only the 100K track.** 500K and 1M exist and are the point of the paper,
  but one 1M-token conversation is roughly ten LongMemEval haystacks and this
  stand builds a vault per question. The 100K track is the one that fits
  today; the same converter reads the others when it is worth the hours.

Source: `huggingface.co/datasets/Mohammadta/BEAM`, `data/100K-*.parquet`.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

DATASET_DIR = Path(__file__).resolve().parent.parent / "cache" / "benchmarks" / "beam"
SOURCE_FILE = DATASET_DIR / "beam-100K.parquet"
CONVERTED_FILE = DATASET_DIR / "beam-100K-questions.json"
SOURCE_URL = (
    "https://huggingface.co/datasets/Mohammadta/BEAM/resolve/main/data/"
    "100K-00000-of-00001.parquet"
)
ABSTENTION = "abstention"
ANSWER_KEYS = ("answer", "ideal_answer", "ideal_response")
SCORED_ABILITIES = (
    ABSTENTION,
    "contradiction_resolution",
    "event_ordering",
    "information_extraction",
    "knowledge_update",
    "multi_session_reasoning",
    "temporal_reasoning",
)
_COORDINATE = re.compile(r"\s*->->\s*\d+\s*,\s*\d+\s*$")
_ANCHOR = "%B-%d-%Y"
ASKED_AFTER_DAYS = 1


class DatasetUnavailable(RuntimeError):
    """The real dataset is not on disk; a synthetic stand-in is forbidden."""


def load_rows(path: Path = SOURCE_FILE) -> list[dict]:
    if not path.exists():
        raise DatasetUnavailable(
            f"BEAM is not cached at {path}. Download the real file from "
            f"{SOURCE_URL} and save it there. This harness never generates a "
            "synthetic stand-in."
        )
    import pyarrow.parquet as pq

    return pq.read_table(path).to_pylist()


def _clean(text: object) -> str:
    return _COORDINATE.sub("", str(text or "")).strip()


def _anchor_of(batch: list) -> datetime | None:
    for turn in batch:
        anchor = str(turn.get("time_anchor") or "")
        if anchor and anchor != "None":
            return datetime.strptime(anchor, _ANCHOR)
    return None


def _turns_of(batch: list) -> list[dict]:
    return [
        {"role": str(turn.get("role") or "user"), "content": _clean(turn.get("content"))}
        for turn in batch
        if _clean(turn.get("content"))
    ]


def _format(when: datetime) -> str:
    return when.strftime("%Y/%m/%d (%a) %H:%M")


def _haystack(chat: list) -> tuple[list[list[dict]], list[str]]:
    sessions, dates = [], []
    for index, batch in enumerate(chat):
        turns = _turns_of(batch)
        anchor = _anchor_of(batch)
        if not turns or anchor is None:
            continue
        sessions.append(turns)
        dates.append(_format(anchor + timedelta(hours=9)))
    return sessions, dates


def _probes(row: dict) -> dict:
    raw = row.get("probing_questions")
    if isinstance(raw, dict):
        return raw
    return ast.literal_eval(str(raw or "{}"))


def _gold(item: dict) -> str:
    parts = (str(item.get(key) or "").strip() for key in ANSWER_KEYS)
    return next((part for part in parts if part), "")


def _question_id(conversation: str, ability: str, index: int) -> str:
    suffix = "_abs" if ability == ABSTENTION else ""
    return f"BEAM-{conversation}-{ability}-{index}{suffix}"


def _answer_for(ability: str, item: dict) -> str:
    """An abstention question is right when nothing is said."""
    if ability == ABSTENTION:
        return ""
    return _gold(item)


def _converted(
    row: dict, ability: str, index: int, item: dict, haystack: tuple
) -> dict:
    sessions, dates = haystack
    asked = datetime.strptime(dates[-1][:10], "%Y/%m/%d") + timedelta(
        days=ASKED_AFTER_DAYS, hours=12
    )
    return {
        "question_id": _question_id(str(row.get("conversation_id")), ability, index),
        "question_type": ability,
        "question": str(item.get("question") or ""),
        "answer": _answer_for(ability, item),
        "question_date": _format(asked),
        "haystack_dates": dates,
        "haystack_session_ids": [
            f"beam-{row.get('conversation_id')}-b{n}" for n in range(len(sessions))
        ],
        "haystack_sessions": sessions,
        "answer_session_ids": [],
        "beam_difficulty": str(item.get("difficulty") or ""),
        "beam_reference_answer": _gold(item),
        "beam_why_unanswerable": str(item.get("why_unanswerable") or ""),
    }


def _usable(ability: str, item: dict) -> bool:
    if not str(item.get("question") or "").strip():
        return False
    return ability == ABSTENTION or bool(_gold(item))


def _scored_probes(probes: dict) -> list[tuple[str, int, dict]]:
    """Every probe of an ability this stand can score, with its ordinal."""
    return [
        (ability, index, item)
        for ability in SCORED_ABILITIES
        for index, item in enumerate(probes.get(ability, []))
    ]


def _questions_of(row: dict) -> list[dict]:
    haystack = _haystack(list(row.get("chat") or []))
    if not haystack[0]:
        return []
    return [
        _converted(row, ability, index, item, haystack)
        for ability, index, item in _scored_probes(_probes(row))
        if _usable(ability, item)
    ]


def converted_questions(rows: list[dict]) -> list[dict]:
    return [question for row in rows for question in _questions_of(row)]


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
