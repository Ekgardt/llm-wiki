"""How much of the evidence a question needs reached the reader.

The stand recorded a first-hit rank and a row count, and neither can tell "one of
four sessions found" from "all four found" — while accuracy on the 500 questions
falls from 0.906 at one needed session to 0.471 at four. LongMemEval labels the
evidence twice: `answer_session_ids` per question, and `has_answer: true` on the
turns that carry it. These functions count both against what retrieval returned,
as recall and as the all-or-nothing acc@k that multi-hop evaluation uses.
Research: `docs/research/2026-09-14-a-memory-stand-that-measures-retrieval.md`.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

# A turn is longer than a chunk more often than not, and retrieval may hand over
# the middle of it rather than its first line. So a turn counts as seen when any
# window of it appears in a retrieved row — measured 2026-09-14 on question
# `gpt4_d84a3211`, whose four evidence turns are 252-368 characters against
# chunks of about 250.
WINDOW_CHARS = 80
WINDOW_STEP = 40
DEPTHS = (12, 24, 48)


def normalized(text: object) -> str:
    """Whitespace collapsed and case folded, so a match survives re-rendering."""
    return " ".join(str(text or "").split()).casefold()


def _row_text(row: Mapping[str, object]) -> str:
    """The retrieved text itself, plus the headings that name its session."""
    parts = [str(row.get(key) or "") for key in ("content", "summary", "title", "path")]
    ancestry = row.get("heading_ancestry")
    if isinstance(ancestry, (list, tuple)):
        parts.extend(str(item) for item in ancestry)
    return normalized("\n".join(parts))


def labelled_sessions(question: Mapping[str, object]) -> set[str]:
    return {str(item) for item in question.get("answer_session_ids") or []}


def _turn_windows(turn: Mapping[str, object]) -> tuple[str, ...]:
    """Overlapping pieces of a turn; any one of them appearing means it was seen."""
    text = normalized(turn.get("content"))
    if len(text) <= WINDOW_CHARS:
        return (text,)
    starts = range(0, len(text) - WINDOW_CHARS + 1, WINDOW_STEP)
    return tuple(text[start : start + WINDOW_CHARS] for start in starts)


def _all_turns(question: Mapping[str, object]) -> list[object]:
    sessions = question.get("haystack_sessions") or []
    return [turn for session in sessions for turn in session]


def _has_text(windows: tuple[str, ...]) -> bool:
    return bool(windows and windows[0])


def evidence_turns(question: Mapping[str, object]) -> list[tuple[str, ...]]:
    """The windows of every turn the dataset flags as carrying the answer."""
    flagged = filter(_is_evidence, _all_turns(question))
    return list(filter(_has_text, map(_turn_windows, flagged)))


def _is_evidence(turn: object) -> bool:
    return isinstance(turn, Mapping) and turn.get("has_answer") is True


def _covered_sessions(sessions: set[str], texts: Sequence[str]) -> set[str]:
    return {session for session in sessions if any(session.casefold() in text for text in texts)}


def _turn_seen(windows: tuple[str, ...], texts: Sequence[str]) -> bool:
    return any(window in text for window in windows for text in texts)


def _covered_turns(turns: Sequence[tuple[str, ...]], texts: Sequence[str]) -> int:
    return sum(1 for windows in turns if _turn_seen(windows, texts))


def _share(part: int, whole: int) -> float | None:
    if not whole:
        return None
    return round(part / whole, 4)


def coverage(question: Mapping[str, object], rows: Sequence[Mapping[str, object]]) -> dict:
    """Session and turn coverage of these rows, as counts, recall and acc@k."""
    texts = [_row_text(row) for row in rows]
    sessions = labelled_sessions(question)
    turns = evidence_turns(question)
    seen_sessions = len(_covered_sessions(sessions, texts))
    seen_turns = _covered_turns(turns, texts)
    return {
        "sessions_labelled": len(sessions),
        "sessions_covered": seen_sessions,
        "session_recall": _share(seen_sessions, len(sessions)),
        "all_sessions": bool(sessions) and seen_sessions == len(sessions),
        "turns_labelled": len(turns),
        "turns_covered": seen_turns,
        "turn_recall": _share(seen_turns, len(turns)),
        "all_turns": bool(turns) and seen_turns == len(turns),
    }


def evidence_in_text(question: Mapping[str, object], text: str) -> dict:
    """How many of the dataset's evidence turns appear in one block of text.

    Written for the prompt the reader actually received. Defined for every
    question type, including the one whose gold is a rubric, because the turns
    are labelled by the dataset (`has_answer`) and not derived from the gold
    string. Asking instead whether the gold string is in the prompt measures the
    shape of the gold: it is absent by construction when the gold was computed
    ("6 days.") or written by the dataset's authors as a description of a good
    answer. See `docs/research/2026-09-18-a-rubric-is-not-a-miss.md`.
    """
    turns = evidence_turns(question)
    seen = _covered_turns(turns, [normalized(text)])
    return {
        "turns_labelled": len(turns),
        "turns_in_text": seen,
        "turn_recall": _share(seen, len(turns)),
        "all_turns": bool(turns) and seen == len(turns),
    }


def coverage_at_depths(
    question: Mapping[str, object],
    rows: Sequence[Mapping[str, object]],
    depths: Sequence[int] = DEPTHS,
) -> dict:
    """Coverage for each prefix of one ranked list: `k12`, `k24`, `k48`."""
    return {f"k{depth}": coverage(question, rows[:depth]) for depth in depths}


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _shares(entries: Sequence[Mapping[str, object]]) -> dict:
    """Recall means and all-or-nothing shares over one group of coverage entries."""
    def numbers(key: str) -> list[float]:
        return [float(entry[key]) for entry in entries if entry.get(key) is not None]

    def flags(key: str) -> list[float]:
        return [float(bool(entry.get(key))) for entry in entries]

    return {
        "n": len(entries),
        "session_recall": _mean(numbers("session_recall")),
        "all_sessions": _mean(flags("all_sessions")),
        "turn_recall": _mean(numbers("turn_recall")),
        "all_turns": _mean(flags("all_turns")),
    }


def _depth_entries(rows: Sequence[Mapping[str, object]]) -> dict[str, list]:
    """Every coverage entry a run recorded, keyed by the depth it was taken at."""
    by_depth: dict[str, list] = {}
    for row in rows:
        for depth, entry in _row_depths(row).items():
            by_depth.setdefault(depth, []).append((row, entry))
    return by_depth


def _row_depths(row: Mapping[str, object]) -> dict:
    depths = dict(row.get("coverage_depths") or {})
    if isinstance(row.get("coverage"), Mapping):
        depths.setdefault("k12", row["coverage"])
    return depths


def _by_type(pairs: Sequence[tuple]) -> dict:
    groups: dict[str, list] = {}
    for row, entry in pairs:
        groups.setdefault(str(row.get("question_type")), []).append(entry)
    report = {name: _shares(entries) for name, entries in sorted(groups.items())}
    report["overall"] = _shares([entry for _row, entry in pairs])
    return report


def aggregate(rows: Sequence[Mapping[str, object]]) -> dict:
    """Coverage by depth and by question type for one run."""
    return {depth: _by_type(pairs) for depth, pairs in sorted(_depth_entries(rows).items())}


def _number(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, int):
        return value
    return 0


def compile_evidence(rows: Sequence[Mapping[str, object]]) -> dict:
    """What the compiler could place of the evidence chunks it asked for.

    A different stage from the coverage above, which measures what search
    returned, and from the report's prompt-evidence figures, which measure what
    the reader saw. All three were read as "coverage" by whoever quoted them,
    and on the recorded run of 2026-09-17 they read 0.912, 1.0 and 0.508 —
    three numbers for one word, at three points of the pipeline. Each is named
    for its stage now. See `docs/research/2026-09-18-a-rubric-is-not-a-miss.md`.
    """
    requested = sum(_number(row, "evidence_requested") for row in rows)
    missed = sum(_number(row, "evidence_missed") for row in rows)
    return {
        "chunks_requested": requested,
        "chunks_missed": missed,
        "chunks_placed_share": _share(requested - missed, requested),
    }


def failure_split(rows: Sequence[Mapping[str, object]]) -> dict:
    """Wrong answers with every evidence turn in hand, and wrong answers without.

    The first is the reader's failure and the second retrieval's, which is what
    decides whether the next change belongs in search or in answer composition.

    `judged` leads, because without it a run written before its judge pass reads
    `{"wrong": 0}` — which is what `lme500.report.json` said on 2026-09-17 about
    a run with 199 wrong answers. Zero wrong and zero judged is silence, and it
    has to look like silence.
    """
    wrong = _wrong_answers(rows)
    in_hand = _with_evidence(wrong)
    return {
        "judged": _judged_count(rows),
        "wrong": len(wrong),
        "evidence_in_hand": in_hand,
        "evidence_missing": len(wrong) - in_hand,
    }


def _wrong_answers(rows: Sequence[Mapping[str, object]]) -> list:
    return [row for row in rows if row.get("judge_correct") is False]


def _judged_count(rows: Sequence[Mapping[str, object]]) -> int:
    return sum(1 for row in rows if isinstance(row.get("judge_correct"), bool))


def _with_evidence(rows: Sequence[Mapping[str, object]]) -> int:
    return sum(1 for row in rows if _all_turns_seen(row))


def _all_turns_seen(row: Mapping[str, object]) -> bool:
    entry = row.get("coverage")
    return isinstance(entry, Mapping) and bool(entry.get("all_turns"))
