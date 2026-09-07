#!/usr/bin/env python3
"""LoCoMo, converted into the shape this stand already runs.

LoCoMo is ten conversations of up to 35 sessions between two people, with
1 986 questions over five categories. Its 446 category-5 questions are
adversarial: they name something the conversation never says, and the dataset
ships the plausible wrong answer a system hallucinates instead of the right
one. Refusing them is the correct behaviour, which is the one thing this
product is built to do and the one thing LongMemEval scores against us.

Rather than a second harness, this converts LoCoMo into the LongMemEval
question shape so `run_longmemeval.py --dataset` runs it unchanged, scorer and
all. A category-5 question takes the `_abs` question-id suffix, which is how
`longmemeval_data.is_abstention` already recognises a question whose right
answer is silence.

## What this conversion decides, and why it is written down

- **Two people become user and assistant.** LoCoMo speakers are both human.
  The first speaker to appear in a conversation is mapped to `user` and the
  second to `assistant`, consistently for that conversation.
- **An image becomes its caption.** A turn with `img_url` carries a
  `blip_caption`; the caption is appended to the text as
  `[image: <caption>]`. A text-only system cannot do better, and saying so is
  the point — the audit below notes that implementations differ here silently.
- **A question is asked after the last session.** LoCoMo dates sessions but
  not questions, so `question_date` is one day after the last session.

## What is known to be wrong with LoCoMo

Read before quoting any number from it. The published audit at
`github.com/dial481/locomo-audit` finds 99 of 1 540 scored questions carry an
incorrect golden answer, which puts a ceiling of 93.57% on any system; that
the 446 adversarial questions have never been properly evaluated in published
results because the evaluation code is broken; and that the usual LLM judge
accepts 62.81% of deliberately wrong but topical answers. So a LoCoMo number
compares systems only as loosely as its judge, and the headline scores
competitors publish are inflated by all three.

That is the reason to run it anyway, and to run the part nobody runs: on the
adversarial subset there is no inflated number to beat, and abstention is
scored by whether the system stayed silent, not by a judge's opinion of a
sentence.

Source: Maharana et al., "Evaluating Very Long-Term Conversational Memory of
LLM Agents" (2024), data at `snap-research/locomo`, CC BY-NC 4.0.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

DATASET_DIR = Path(__file__).resolve().parent.parent / "cache" / "benchmarks" / "locomo"
SOURCE_FILE = DATASET_DIR / "locomo10.json"
CONVERTED_FILE = DATASET_DIR / "locomo_s.json"
SOURCE_URL = (
    "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
)
EXPECTED_CONVERSATIONS = 10
EXPECTED_QUESTIONS = 1986
ADVERSARIAL_CATEGORY = 5
CATEGORY_NAMES = {
    1: "multi-hop",
    2: "temporal-reasoning",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}
_SESSION = re.compile(r"^session_(\d+)$")
_WHEN = re.compile(r"^(\d{1,2}):(\d{2}) (am|pm) on (\d{1,2}) (\w+), (\d{4})$")
_MONTHS = (
    "January February March April May June July August September October "
    "November December"
).split()


class DatasetUnavailable(RuntimeError):
    """The real dataset is not on disk; a synthetic stand-in is forbidden."""


def load_source(path: Path = SOURCE_FILE) -> list[dict]:
    if not path.exists():
        raise DatasetUnavailable(
            f"LoCoMo is not cached at {path}. Download the real file (2.7 MB) "
            f"from {SOURCE_URL} and save it there. This harness never "
            "generates a synthetic stand-in."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise DatasetUnavailable("LoCoMo must be a non-empty JSON array")
    return data


def _hour24(hour: int, meridiem: str) -> int:
    if meridiem == "am":
        return 0 if hour == 12 else hour
    return 12 if hour == 12 else hour + 12


def parse_when(value: str) -> datetime:
    """`1:56 pm on 8 May, 2023` — the only date format LoCoMo writes."""
    match = _WHEN.match(str(value).strip())
    if match is None:
        raise ValueError(f"unrecognised LoCoMo timestamp: {value!r}")
    hour, minute, meridiem, day, month, year = match.groups()
    return datetime(
        int(year),
        _MONTHS.index(month) + 1,
        int(day),
        _hour24(int(hour), meridiem),
        int(minute),
    )


def format_when(when: datetime) -> str:
    """The `2023/05/08 (Mon) 13:56` form the LongMemEval shape carries."""
    return when.strftime("%Y/%m/%d (%a) %H:%M")


def _turn_text(turn: dict) -> str:
    text = str(turn.get("text") or "").strip()
    caption = str(turn.get("blip_caption") or "").strip()
    if not caption:
        return text
    return f"{text}\n[image: {caption}]".strip()


def _speakers_in(turns: list) -> list[str]:
    return [str(turn.get("speaker") or "") for turn in turns]


def _spoken_names(conversation: dict, ordered: list[str]) -> list[str]:
    """Every speaker, in the order they first say something."""
    names: list[str] = []
    for key in ordered:
        names.extend(_speakers_in(conversation.get(key, [])))
    return [name for name in dict.fromkeys(names) if name]


def _role_for(index: int) -> str:
    return "user" if index == 0 else "assistant"


def _roles_of(conversation: dict, ordered: list[str]) -> dict[str, str]:
    """Whoever speaks first is the user; the other one is the assistant."""
    speakers = _spoken_names(conversation, ordered)
    return {name: _role_for(index) for index, name in enumerate(speakers)}


def _ordered_sessions(conversation: dict) -> list[str]:
    numbered = [
        (int(_SESSION.match(key).group(1)), key)
        for key in conversation
        if _SESSION.match(key)
    ]
    return [key for _number, key in sorted(numbered)]


def _session_turns(turns: list, roles: dict[str, str]) -> list[dict]:
    return [
        {"role": roles.get(str(turn.get("speaker")), "user"), "content": _turn_text(turn)}
        for turn in turns
        if _turn_text(turn)
    ]


def _haystack(conversation: dict) -> tuple[list[list[dict]], list[str], list[str]]:
    ordered = _ordered_sessions(conversation)
    roles = _roles_of(conversation, ordered)
    sessions, dates, identifiers = [], [], []
    for key in ordered:
        turns = _session_turns(conversation.get(key, []), roles)
        if not turns:
            continue
        sessions.append(turns)
        dates.append(format_when(parse_when(conversation[f"{key}_date_time"])))
        identifiers.append(key)
    return sessions, dates, identifiers


def _question_id(sample_id: str, index: int, category: int) -> str:
    suffix = "_abs" if category == ADVERSARIAL_CATEGORY else ""
    return f"{sample_id}_{index}{suffix}"


def _answer_of(record: dict) -> str:
    """An adversarial question has no answer; silence is the answer."""
    if record.get("category") == ADVERSARIAL_CATEGORY:
        return ""
    return str(record.get("answer", ""))


def _asked_after(dates: list[str]) -> str:
    latest = max(datetime.strptime(value[:10], "%Y/%m/%d") for value in dates)
    return format_when(latest + timedelta(days=1, hours=12))


def converted_questions(data: list[dict]) -> list[dict]:
    """Every LoCoMo question in the shape the LongMemEval harness reads."""
    questions = []
    for conversation in data:
        sessions, dates, identifiers = _haystack(conversation["conversation"])
        sample_id = str(conversation["sample_id"])
        asked = _asked_after(dates)
        for index, record in enumerate(conversation.get("qa", [])):
            questions.append(
                {
                    "question_id": _question_id(sample_id, index, record.get("category")),
                    "question_type": CATEGORY_NAMES.get(
                        record.get("category"), "unknown"
                    ),
                    "question": str(record.get("question", "")),
                    "answer": _answer_of(record),
                    "question_date": asked,
                    "haystack_dates": dates,
                    "haystack_session_ids": identifiers,
                    "haystack_sessions": sessions,
                    "answer_session_ids": [],
                    "locomo_evidence": list(record.get("evidence") or []),
                    "locomo_adversarial_answer": str(
                        record.get("adversarial_answer", "")
                    ),
                }
            )
    return questions


def convert(source: Path = SOURCE_FILE, target: Path = CONVERTED_FILE) -> Path:
    questions = converted_questions(load_source(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(questions, ensure_ascii=False), encoding="utf-8"
    )
    return target


def main() -> int:
    target = convert()
    questions = json.loads(target.read_text(encoding="utf-8"))
    adversarial = sum(1 for item in questions if item["question_id"].endswith("_abs"))
    print(f"wrote {len(questions)} question(s) to {target}")
    print(f"  adversarial (answer is silence): {adversarial}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
