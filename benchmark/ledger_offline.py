"""What a ledger would have counted on a recorded run, set beside the gold.

The recorded runs hold no model-written ledger records (the stand keyed turns only
when asked to, into a disposable state root), so this stand-in posts one record per
user turn that names the counted kind — thing: the words before the kind in that
sentence, day: the session day, quantity: a number just before the phrase — over
the whole haystack, which is the complete enumeration a top-k reader never has,
and then counts by code with the product's own `ledger.count`. It measures the
counting half with a zero-model extractor; what the model's records add is what
only a run can settle. The trigger is the question text (`asks_to_aggregate`,
`counted_kind`), never a benchmark label; the labels are read only to score.

    uv run python benchmark/ledger_offline.py --judged <run>.judged.jsonl --staging <dir>

Research: `docs/research/2026-09-22-a-ledger-of-things-and-events-posted-once.md`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ledger  # noqa: E402
from aggregation_pass import asks_to_aggregate, counted_kind  # noqa: E402

NUMBER_WORDS = {
    word: float(index)
    for index, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
    )
}
_DIGITS = re.compile(r"\$?(\d[\d,]*(?:\.\d+)?)")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
# A year in a hypothesis is a date, not the number the reader stated.
_YEAR = range(1900, 2101)
VERDICTS = ("fixed", "kept", "broke", "still_wrong", "undecided", "out_of_scope")
_OUTCOME = {
    (True, True): "kept",
    (False, True): "fixed",
    (True, False): "broke",
    (False, False): "still_wrong",
}
THING_WORDS = 2
SPLITS = ("all", "tune", "decide")


def _digits_in(text: object) -> list[float]:
    return [float(match.group(1).replace(",", "")) for match in _DIGITS.finditer(str(text or ""))]


def _number_words_in(text: object) -> list[float]:
    words = (word.casefold() for word in _WORD.findall(str(text or "")))
    return [NUMBER_WORDS[word] for word in words if word in NUMBER_WORDS]


def _number_in_words(text: object) -> float | None:
    found = _number_words_in(text)
    if not found:
        return None
    return found[0]


def gold_number(text: object) -> float | None:
    """The number a gold answer states first, in digits or in words."""
    leading = _DIGITS.match(str(text or "").strip())
    if leading:
        return float(leading.group(1).replace(",", ""))
    return _number_in_words(text)


def stated_number(text: object) -> float | None:
    """The first number the reader's answer states that is not a year."""
    values = [value for value in _digits_in(text) if value not in _YEAR]
    if values:
        return values[0]
    return _number_in_words(text)


def day_of(date_text: str) -> str:
    return str(date_text).split()[0].replace("/", "-")


def _quantity_of(word: str) -> float | None:
    lowered = word.casefold()
    if lowered in NUMBER_WORDS:
        return NUMBER_WORDS[lowered]
    digits = _DIGITS.fullmatch(lowered)
    if digits:
        return float(digits.group(1).replace(",", ""))
    return None


def _quantity_before(words: Sequence[str], index: int) -> float | None:
    if index == 0:
        return None
    return _quantity_of(words[index - 1])


def _phrase(words: Sequence[str], index: int) -> str:
    return " ".join(words[max(0, index - THING_WORDS) : index + 1])


def mentions(sentence: str, kind: str) -> list[tuple[str, float | None]]:
    """(thing phrase, stated quantity) for each word of the sentence that is the kind."""
    words = _WORD.findall(sentence)
    hits = [index for index, word in enumerate(words) if ledger.canonical_kind(word) == kind]
    return [(_phrase(words, index), _quantity_before(words, index)) for index in hits]


def _session_user_turns(session_index: int, session: Sequence[Mapping], day: str) -> Iterable[tuple]:
    """(session index, turn index, day, text) for the user turns of one session."""
    for turn_index, turn in enumerate(session):
        if turn.get("role") == "user":
            yield session_index, turn_index, day, str(turn.get("content") or "")


def _user_turns(question: Mapping[str, object]) -> Iterable[tuple]:
    sessions = question.get("haystack_sessions") or []
    dates = question.get("haystack_dates") or []
    for session_index, (session, date_text) in enumerate(zip(sessions, dates)):
        yield from _session_user_turns(session_index, session, day_of(str(date_text)))


def _record(kind: str, thing: str, quantity: float | None, day: str, session: int, turn: int) -> ledger.Record:
    return ledger.Record(
        kind=kind,
        thing=ledger.canonical(thing),
        event="",
        day=day,
        quantity=quantity,
        by_user=True,
        dated=False,
        source_path=f"knowledge/daily/{day}.md",
        source_sha256="",
        byte_start=session * 1000 + turn,
        byte_end=session * 1000 + turn + 1,
        span_sha256="",
    )


def _turn_records(kind: str, day: str, session: int, turn: int, text: str) -> list[ledger.Record]:
    found: list[ledger.Record] = []
    for sentence in _SENTENCE.split(text):
        found.extend(_record(kind, thing, quantity, day, session, turn) for thing, quantity in mentions(sentence, kind))
    return found


def standin_records(question: Mapping[str, object], kind: str) -> list[ledger.Record]:
    """One record per user turn naming the kind, from the whole haystack."""
    found: list[ledger.Record] = []
    for session, turn, day, text in _user_turns(question):
        found.extend(_turn_records(kind, day, session, turn, text))
    return found


def question_window(question_text: str, question_date: str) -> tuple[str, str] | None:
    """The stretch of days the question names, through the product's own anchor."""
    import temporal_anchor

    anchor = date.fromisoformat(day_of(question_date))
    return temporal_anchor.window(question_text, anchor)


def counted(
    question: Mapping[str, object], kind: str
) -> tuple[ledger.LedgerCount | None, float | None]:
    """(the stand-in ledger's count, the sum of stated quantities) for the kind."""
    if not kind:
        return None, None
    connection = sqlite3.connect(":memory:")
    ledger.ensure_table(connection)
    ledger.post(connection, standin_records(question, kind))
    window = question_window(str(question["question"]), str(question["question_date"]))
    return ledger.count(connection, kind, window), ledger.sum_quantities(connection, kind, window)


def figure(count: ledger.LedgerCount | None, quantity_sum: float | None) -> float | None:
    """The one number the ledger answers with: stated quantities when the person gave
    any, else the distinct things. "How many playlists do I have" is answered by the
    twenty the person stated, not by the mentions of playlists."""
    if count is None or count.records == 0:
        return None
    if quantity_sum:
        return quantity_sum
    return float(count.things)


def _scope(gold: float | None, count: ledger.LedgerCount | None) -> str | None:
    """Why no verdict can be given, or None when one can."""
    if gold is None or count is None:
        return "out_of_scope"
    if count.records == 0:
        return "undecided"
    return None


def verdict(judge_correct: bool, gold: float | None, count: ledger.LedgerCount | None, answer: float | None) -> str:
    """One of `VERDICTS`: what the ledger's number would have done to this answer."""
    undecidable = _scope(gold, count)
    if undecidable is not None:
        return undecidable
    return _OUTCOME[(judge_correct, answer == gold)]


def split_of(question_id: str) -> str:
    """LongMemEval: question-id digest parity; LoCoMo: conversations 1-5 tune, the rest decide."""
    conversation = re.match(r"conv-(\d+)_", question_id)
    if conversation:
        return _locomo_split(int(conversation.group(1)))
    parity = int(hashlib.sha256(question_id.encode("utf-8")).hexdigest(), 16) % 2
    return SPLITS[1 + parity]


def _locomo_split(conversation: int) -> str:
    if conversation <= 5:
        return "tune"
    return "decide"


def _count_fields(count: ledger.LedgerCount | None) -> dict[str, object]:
    if count is None:
        return {"records": 0, "things": None, "events": None, "tier": None}
    return {"records": count.records, "things": count.things, "events": count.events, "tier": count.tier}


def _row_result(row: Mapping[str, object], question: Mapping[str, object]) -> dict[str, object]:
    kind = counted_kind(str(row["question"]))
    count, quantity_sum = counted(question, kind)
    gold = gold_number(row.get("gold"))
    stated = stated_number(row.get("hypothesis"))
    correct = bool(row.get("judge_correct"))
    answer = figure(count, quantity_sum)
    return {
        "question_id": row["question_id"],
        "question_type": row.get("question_type"),
        "split": split_of(str(row["question_id"])),
        "kind": kind,
        "gold": gold,
        "stated": stated,
        "judge_correct": correct,
        **_count_fields(count),
        "quantity_sum": quantity_sum,
        "figure": answer,
        "verdict": verdict(correct, gold, count, answer),
        "reconciled": _reconciled(stated, answer),
    }


def _reconciled(stated: float | None, answer: float | None) -> bool | None:
    """Whether the reader's number equals the ledger's figure; None when either is missing."""
    if stated is None or answer is None:
        return None
    return stated == answer


def _is_counting(row: Mapping[str, object]) -> bool:
    return asks_to_aggregate(str(row.get("question"))) and not row.get("is_abstention")


def counting_rows(judged: Path) -> list[dict]:
    lines = judged.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    return [row for row in rows if _is_counting(row)]


def evaluate(judged: Path, staging: Path) -> list[dict[str, object]]:
    results = []
    for row in counting_rows(judged):
        question = json.loads((staging / f"{row['question_id']}.question.json").read_text(encoding="utf-8"))
        results.append(_row_result(row, question))
    return results


def _in_split(result: Mapping[str, object], name: str) -> bool:
    return name == "all" or result["split"] == name


def _verdict_counts(results: Iterable[Mapping[str, object]]) -> dict[str, int]:
    tally = Counter(str(result["verdict"]) for result in results)
    return {name: tally.get(name, 0) for name in VERDICTS}


def table(results: Sequence[Mapping[str, object]]) -> dict[str, dict[str, int]]:
    """Verdict counts, overall and per split."""
    return {name: _verdict_counts(r for r in results if _in_split(r, name)) for name in SPLITS}


def reconciliation_table(results: Sequence[Mapping[str, object]]) -> dict[str, int]:
    """How often the reconcile flag would fire on wrong answers and on right ones."""
    tally = Counter((bool(r["judge_correct"]), r["reconciled"]) for r in results)
    return {
        "wrong_flagged": tally[(False, False)],
        "wrong_unflagged": tally[(False, True)],
        "right_flagged": tally[(True, False)],
        "right_unflagged": tally[(True, True)],
    }


def _print(results: Sequence[Mapping[str, object]]) -> None:
    print(json.dumps({"verdicts": table(results), "reconcile": reconciliation_table(results)}, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--judged", required=True, type=Path)
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    results = evaluate(args.judged, args.staging)
    _print(results)
    if args.out:
        args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
