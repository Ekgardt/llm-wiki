"""Deterministic scoring for the LongMemEval run (MEM-10).

The paper's official protocol judges answers with a GPT-4o prompt; nothing on
this machine can reproduce that judge, so the scores here are deterministic
text metrics and are labelled as such wherever they are reported:

- ``em``       — SQuAD-style normalized exact match (whole answer).
- ``contains`` — the normalized gold answer appears inside the normalized
                 hypothesis. This is the primary accuracy figure: verified
                 grounded answers quote evidence sentences, so whole-string
                 EM under-credits an answer that plainly states the fact.
- ``f1``       — token-level F1 between gold and hypothesis.
- abstention questions (`_abs` ids) are correct iff the product abstained.

These numbers are NOT comparable one-to-one with LLM-judge numbers such as
Mem0's published 93.4%; the research note carries that caveat.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_data  # noqa: E402

_ARTICLES = frozenset({"a", "an", "the"})
_NON_ALNUM = re.compile(r"[^0-9a-zЀ-ӿ]+")

PROVIDER_ERROR_KINDS = frozenset(
    {"provider_no_response", "provider_invalid_json", "provider_deadline"}
)

# One LongMemEval type is graded against a rubric instead of against a fact, and
# every text metric in this file is undefined there. The authors' own template
# says so: "I will give you a question, a rubric for desired personalized
# response, and a response from a model ... The model does not need to reflect
# all the points in the rubric" (`benchmark/longmemeval_official.py::PREFERENCE`,
# copied character for character from their `src/evaluation/evaluate_qa.py`).
#
# A gold of that shape was written by the dataset's authors, not said by anyone
# in the sessions — "The user would prefer responses that suggest resources
# specifically tailored to Adobe Premiere Pro". No answer contains it and no
# prompt can either, so `contains`, `em`, `f1` and the prompt-evidence signal
# all read 0 there whatever the system does. Measured on the recorded run of
# 2026-09-17: 0 of 30 by containment, and the judge called right every one of
# the three rows it managed to grade.
#
# An undefined metric reports nothing. It never reports a miss.
# See `docs/research/2026-09-18-a-rubric-is-not-a-miss.md`.
RUBRIC_CATEGORIES = frozenset({"single-session-preference"})


def gold_is_rubric(row: dict) -> bool:
    """Whether this row's gold describes a good answer instead of being one."""
    return str(row.get("category") or row.get("question_type")) in RUBRIC_CATEGORIES


# The rows whose right answer is silence: the dataset's abstention questions,
# and the stand's twins — answerable questions re-asked with their evidence
# sessions removed, so refusing is right and answering is an invention.
SILENCE_CATEGORIES = frozenset({"abstention", longmemeval_data.TWIN_CATEGORY})


def silence_expected(row: dict) -> bool:
    return bool(row.get("is_abstention")) or str(row.get("category")) in SILENCE_CATEGORIES


def _gold_is_an_explanation(row: dict) -> bool:
    """An abstention's gold says why the question cannot be answered.

    The authors' template hands the judge "an unanswerable question, an
    explanation, and a response from a model"
    (`longmemeval_official.ABSTENTION`). Nobody in the sessions said that
    sentence either; a twin's gold is empty for the same reason.
    """
    return silence_expected(row)


def has_verbatim_gold(row: dict) -> bool:
    """Whether this row's gold is text somebody actually said in a session.

    Only then can "did the gold reach the prompt" be asked at all. Two shapes
    fail it — a rubric and an abstention's explanation — and on the recorded run
    of 2026-09-17 both read a flat 0 for that reason and no other: 0 of 30
    preference rows and 0 of 30 abstention rows.
    """
    return not gold_is_rubric(row) and not _gold_is_an_explanation(row)


def normalize(text: object) -> str:
    tokens = _NON_ALNUM.sub(" ", str(text).casefold()).split()
    return " ".join(token for token in tokens if token not in _ARTICLES)


def exact_match(gold: object, hypothesis: object) -> bool:
    return normalize(gold) == normalize(hypothesis) and bool(normalize(gold))


_PARENTHETICAL = re.compile(r"\(([^)]*)\)")


_ALIAS_LEAD = re.compile(r"^\s*(?:or|i\.e\.|ie|aka|a\.k\.a\.)\s+", re.IGNORECASE)


def _alias_form(group: str) -> str:
    """A parenthetical alias without the word that introduces it.

    LongMemEval writes the second form of a value as ``(or 25:50)``. Keeping
    the ``or`` in the variant made the alias unmatchable: the correct answer
    ``The user's personal best time was 25:50`` scored zero against gold
    ``25 minutes and 50 seconds (or 25:50)`` in the 2026-08-28 run, because no
    hypothesis says ``or`` before the number.
    """
    return _ALIAS_LEAD.sub("", group)


def gold_variants(gold: object) -> list[str]:
    """The gold string plus its parenthetical alias forms, normalized.

    LongMemEval gold answers often carry both a long form and an alias —
    ``University of California, Los Angeles (UCLA)`` — and a correct grounded
    answer legitimately states only one of them. Each parenthetical group and
    the text without it count as variants; empty variants are dropped.
    """
    text = str(gold)
    variants = [text, _PARENTHETICAL.sub(" ", text)]
    variants.extend(_alias_form(group) for group in _PARENTHETICAL.findall(text))
    normalized = [normalize(variant) for variant in variants]
    return sorted({variant for variant in normalized if variant})


def contains_answer(gold: object, hypothesis: object) -> bool:
    normalized_hypothesis = normalize(hypothesis)
    if not normalized_hypothesis:
        return False
    variants = gold_variants(gold)
    return any(variant in normalized_hypothesis for variant in variants)


def token_f1(gold: object, hypothesis: object) -> float:
    gold_tokens = normalize(gold).split()
    hypothesis_tokens = normalize(hypothesis).split()
    if not gold_tokens or not hypothesis_tokens:
        return 0.0
    overlap = sum((Counter(gold_tokens) & Counter(hypothesis_tokens)).values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(hypothesis_tokens)
    recall = overlap / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


def declined_to_answer(result: dict) -> bool:
    """Whether the system refused, on any question, abstention-labelled or not.

    Recorded on every row rather than only on `_abs` questions, because the
    abstention that costs the score is the one on a question that *did* have an
    answer. Without it the split in `_abstention_split` sees nothing.
    """
    return result.get("status") not in {"answered", None} and not result.get("error")


def _judge_verdicts(rows: list[dict]) -> list[float]:
    return [
        float(row["judge_correct"])
        for row in rows
        if isinstance(row.get("judge_correct"), bool)
    ]


def _judge_accuracy_of(rows: list[dict]) -> float | None:
    """The judge's mean where it spoke, or None where it never ran."""
    verdicts = _judge_verdicts(rows)
    if not verdicts:
        return None
    return round(sum(verdicts) / len(verdicts), 4)


def score_question(result: dict) -> dict:
    """Attach deterministic metrics to one per-question result record."""
    if silence_expected(result):
        return _scored_abstention(result)
    if gold_is_rubric(result):
        return _scored_rubric(result)
    gold = result.get("gold", "")
    hypothesis = result.get("hypothesis", "")
    return {
        **result,
        "em": exact_match(gold, hypothesis),
        "contains": contains_answer(gold, hypothesis),
        "f1": round(token_f1(gold, hypothesis), 4),
        "correct": contains_answer(gold, hypothesis),
        "abstained": declined_to_answer(result),
    }


def _scored_rubric(result: dict) -> dict:
    """No text metric speaks here, so this row claims none of them.

    `abstained` is still recorded: whether the system refused is a fact about
    the run, not a comparison against the gold text.
    """
    return {
        **result,
        "em": None,
        "contains": None,
        "f1": None,
        "correct": None,
        "abstained": declined_to_answer(result),
    }


def _scored_abstention(result: dict) -> dict:
    abstained = declined_to_answer(result)
    return {
        **result,
        "em": abstained,
        "contains": abstained,
        "f1": float(abstained),
        "correct": abstained,
        "abstained": abstained,
    }


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _share(part: int, whole: int) -> float | None:
    if not whole:
        return None
    return round(part / whole, 4)


def _metric_values(rows: list[dict], key: str) -> list[float]:
    return [float(row[key]) for row in rows if row.get(key) is not None]


def is_provider_failure(row: dict) -> bool:
    """The provider never answered, so the memory system was never graded."""
    return row.get("error_kind") in PROVIDER_ERROR_KINDS


# A harness failure is not a wrong answer, and counting it as one is worse than
# counting a provider failure that way: the provider at least ran.
#
# Measured 2026-08-29: a `--workdir` that did not exist failed all 50 questions
# in about two seconds each on a `mkdtemp` traceback, and the report read
# `accuracy 0.0` with `provider_failures 0` in every category — a harness that
# never started, presented as a memory system that answered everything wrongly.
# A number that cannot tell those apart is worse than no number.
HARNESS_ERROR_KINDS = frozenset({"harness_failure"})


def is_harness_failure(row: dict) -> bool:
    """The run never reached the product, so nothing about it was measured."""
    return row.get("error_kind") in HARNESS_ERROR_KINDS


def is_ungraded(row: dict) -> bool:
    """No answer was produced, by either side, so there is nothing to grade."""
    return is_provider_failure(row) or is_harness_failure(row)


def _flags(rows: list[dict], key: str) -> list[float]:
    return [float(bool(row.get(key))) for row in rows]


def _text_scored(scored: list[dict]) -> list[dict]:
    """The rows a text metric can speak about; a rubric gold is not one of them."""
    return [row for row in scored if row.get("correct") is not None]


def _quality_metrics(scored: list[dict]) -> dict:
    """Accuracy over the rows the metric applies to, with that count beside it.

    `text_scored` is the denominator and `text_not_applicable` is what was held
    out, so the figure can never be read as if it covered every question.
    """
    graded = _text_scored(scored)
    return {
        "text_scored": len(graded),
        "text_not_applicable": len(scored) - len(graded),
        "accuracy": _mean(_flags(graded, "correct")),
        "em": _mean(_flags(graded, "em")),
        "f1": _mean(_metric_values(graded, "f1")),
    }


def _cost_metrics(rows: list[dict]) -> dict:
    return {
        "mean_prompt_chars": _mean(_metric_values(rows, "prompt_chars")),
        # The number the published figures are: an LLM judge's verdict, not a
        # substring test. `accuracy` here is `contains_answer`, which cannot
        # match a free-text answer and can never match a preference gold at all
        # — measured 2026-09-01, `single-session-preference` read 0.0000 by
        # containment and 0.25 by the judge on the same rows. Present when the
        # judged rows were folded back in, `None` when they were not.
        "judge_accuracy": _judge_accuracy_of(rows),
        "mean_est_prompt_tokens": _mean(_metric_values(rows, "est_prompt_tokens")),
        "mean_est_total_prompt_tokens": _mean(
            _metric_values(rows, "est_total_prompt_tokens")
        ),
        "mean_retrieve_seconds": _mean(_metric_values(rows, "retrieve_seconds")),
        "mean_answer_seconds": _mean(_metric_values(rows, "answer_seconds")),
        "mean_total_seconds": _mean(_metric_values(rows, "total_seconds")),
        "correct_per_million_tokens": _correct_per_million_tokens(rows),
    }


def _correct_per_million_tokens(rows: list[dict]) -> float | None:
    """Right answers bought per million prompt tokens spent.

    Accuracy alone chose the widest window this stand has measured; economy
    alone would have chosen the narrowest and weakest. Neither is the quantity
    that matters, and until 2026-09-06 nobody computed the one that is.

    On the 2026-09-03 sweep it reads 86.8, 52.7, 20.4 and 15.3 for answer
    budgets of 12 288, 32 768, 122 880 and 262 144 — monotone, and it reverses
    the reading taken from accuracy alone: the peak by accuracy is four times
    worse per token than the narrowest window.

    `None` where the judge never spoke, because a ratio without a numerator is
    not a small number, it is no number.
    """
    if not _judge_verdicts(rows):
        return None
    spent = _tokens_spent(rows)
    if not spent:
        return None
    correct = sum(1 for row in rows if row.get("judge_correct") is True)
    return round(correct / spent * 1_000_000, 2)


def _tokens_spent(rows: list[dict]) -> float:
    values = _metric_values(rows, "est_total_prompt_tokens")
    return float(sum(values))


# An abstention means two opposite things and the report has never said which.
#
# Measured 2026-08-28 at n=50: 26 of 50 answers were `insufficient_evidence`
# and accuracy when the system answers is 14 of 18 = 0.78, so refusal binds
# the score rather than error. But an abstention with a labelled answer session
# among the retrieved candidates is a calibration failure — the evidence was
# there and the answerer declined it — while one without is retrieval doing the
# refusing, and the abstention was correct. The two need opposite work, and
# choosing between them without this split is guessing.
#
# `answer_sessions_retrieved` is written by the worker from the dataset's own
# `answer_session_ids`, not by searching for the gold string: a gold answer is
# often a word like `2018` and a substring search over evidence would confirm
# itself.
def _abstained(row: dict) -> bool:
    return bool(row.get("abstained"))


def _had_the_evidence(row: dict) -> bool:
    return int(row.get("answer_sessions_retrieved") or 0) > 0


def _count(rows: list[dict], predicate) -> int:
    return sum(1 for row in rows if predicate(row))


def _abstention_split(rows: list[dict]) -> dict:
    """Abstentions that had the answer in front of them, and those that did not."""
    abstained = [row for row in rows if _abstained(row)]
    with_evidence = _count(abstained, _had_the_evidence)
    return {
        "abstained": len(abstained),
        "abstained_with_answer_retrieved": with_evidence,
        "abstained_without_answer_retrieved": len(abstained) - with_evidence,
        "answer_retrieved": _count(rows, _had_the_evidence),
    }


def _prompted(rows: list[dict]) -> list[dict]:
    """Rows where a prompt was actually built, so a prompt signal means something."""
    return [row for row in rows if "gold_in_prompt" in row]


def _gold_text_seen(row: dict) -> bool:
    return row.get("gold_in_prompt") is True


def _evidence_seen(row: dict) -> bool:
    return row.get("evidence_in_prompt") is True


def _verbatim_gold_rows(rows: list[dict]) -> list[dict]:
    """The rows whose gold is a span somebody said, so a prompt could carry it."""
    return [row for row in rows if has_verbatim_gold(row)]


def _evidence_measured(rows: list[dict]) -> list[dict]:
    """The rows that recorded the dataset's evidence turns; runs before 2026-09-18 did not."""
    return [row for row in rows if row.get("evidence_turns_labelled")]


def _prompt_evidence(rows: list[dict]) -> dict:
    """What of the question's evidence reached the prompt the reader saw.

    Two figures, each with the denominator it was taken over, because they
    answer different questions and one of them is unanswerable for some types.

    `gold_text_in_prompt` is the row's `gold_in_prompt` under a name that says
    what it measures: the gold string appeared in the prompt word for word. It
    is reported only over the rows whose gold is a span somebody said. A rubric
    gold and an abstention's explanation are held out — the dataset's authors
    wrote both, so no prompt can contain them — and even among the rest it is a
    floor: on the recorded run of 2026-09-17, 101 answers the judge called right
    had no gold text in the prompt, because their golds are computed ("6 days.")
    or restated.

    `evidence_in_prompt` is the dataset's own `has_answer` turns reaching the
    prompt, which is defined for every type. It is reported over the rows that
    recorded it; runs from before 2026-09-18 did not, and their figure is None
    rather than zero. See `docs/research/2026-09-18-a-rubric-is-not-a-miss.md`.
    """
    prompted = _prompted(rows)
    verbatim = _verbatim_gold_rows(prompted)
    measured = _evidence_measured(prompted)
    gold_seen = _count(verbatim, _gold_text_seen)
    evidence_seen = _count(measured, _evidence_seen)
    return {
        "gold_text_in_prompt": gold_seen,
        "gold_text_applicable": len(verbatim),
        "gold_text_not_applicable": len(prompted) - len(verbatim),
        "gold_text_in_prompt_share": _share(gold_seen, len(verbatim)),
        "evidence_in_prompt": evidence_seen,
        "evidence_measured": len(measured),
        "evidence_in_prompt_share": _share(evidence_seen, len(measured)),
    }


# A refusal is a decision, and a decision has to be measured in both directions.
#
# `judge_accuracy` can only report a refusal as a wrong answer.
# `longmemeval_judge.needs_judging` never sends one to the judge, so the
# deterministic substring test stands in and scores the empty hypothesis zero.
# The arithmetic is right — a refusal on a question that had an answer is a loss
# — but until now no field anywhere said how many of the losses were refusals,
# or whether those refusals had the evidence in front of them. Measured on the
# recorded run of 2026-09-18: 26 of the 75 losses on the 470 answerable
# questions are refusals, and 13 of those had the gold string in the prompt word
# for word. `longmemeval_coverage.failure_split` cannot see them either, because
# it counts rows the judge called wrong and a refusal has no judge verdict.
#
# The other direction has no published competitor number at all: on the 30
# questions whose right answer is silence we are silent 26 times, and a system
# that answered everything would turn those 26 into 26 inventions.
#
# Nothing here reads a judge verdict, so the section says the same thing in the
# report written before the judge pass and in the one written after.
# See `docs/research/2026-09-19-a-number-names-its-stand.md`.
def _graded_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if not is_ungraded(row)]


def _silence_expected(rows: list[dict]) -> list[dict]:
    """The questions whose right answer is silence; their gold is an explanation."""
    return [row for row in rows if _gold_is_an_explanation(row)]


def _answerable(rows: list[dict]) -> list[dict]:
    return [row for row in rows if not _gold_is_an_explanation(row)]


def _refused(rows: list[dict]) -> list[dict]:
    return [row for row in rows if declined_to_answer(row)]


def _answered(row: dict) -> bool:
    return row.get("status") == "answered"


def _refusal_evidence(refused: list[dict]) -> dict:
    """What the refusing reader had in front of it, each over its own denominator.

    A refusal that never built a prompt — retrieval returned nothing to read —
    falls out of both denominators rather than counting as evidence it did not
    have.
    """
    prompted = _prompted(refused)
    measured = _evidence_measured(prompted)
    verbatim = _verbatim_gold_rows(prompted)
    return {
        "refused_with_evidence_in_prompt": _count(measured, _evidence_seen),
        "refused_evidence_measured": len(measured),
        "refused_with_gold_text_in_prompt": _count(verbatim, _gold_text_seen),
        "refused_gold_text_applicable": len(verbatim),
    }


def _silence_calibration(rows: list[dict]) -> dict:
    """The refusal decision on the questions that ask for silence, both ways.

    Refused and answered are counted apart rather than one subtracted from the
    other: a row that failed a gate without either answering or declining is
    neither, and naming it as the opposite error would be an invention.
    """
    return {
        "silence_expected": len(rows),
        "refused_when_silence_expected": _count(rows, declined_to_answer),
        "answered_when_silence_expected": _count(rows, _answered),
    }


def abstention_calibration(rows: list[dict]) -> dict:
    """Both directions of the refusal decision, each over the rows it applies to."""
    graded = _graded_rows(rows)
    answerable = _answerable(graded)
    refused = _refused(answerable)
    return {
        "answerable": len(answerable),
        "refused": len(refused),
        **_refusal_evidence(refused),
        **_silence_calibration(_silence_expected(graded)),
        **twin_calibration(graded),
    }


# A twin measures the other direction on the very same question: its evidence
# sessions are gone from the haystack, so refusing is right (AgentAbstain's
# paired accuracy; RESEARCH-B §5). A pair is right when the original was
# answered correctly and the twin was refused. Each figure carries the
# denominator it was taken over, as every line of this block does.
# See `docs/research/2026-09-22-quote-then-answer-and-a-refusal-calibrated-on-twins.md`.
def _is_twin(row: dict) -> bool:
    return str(row.get("category")) == longmemeval_data.TWIN_CATEGORY


def _row_is_correct(row: dict) -> bool:
    """The judge's word where it spoke, the text score where it did not."""
    verdict = row.get("judge_correct")
    if isinstance(verdict, bool):
        return verdict
    return score_question(row).get("correct") is True


def _pair_is_right(twin: dict, originals: dict[str, dict]) -> bool:
    original = originals.get(longmemeval_data.original_of(str(twin.get("question_id"))))
    return original is not None and _row_is_correct(original) and declined_to_answer(twin)


def _originals(rows: list[dict]) -> dict[str, dict]:
    return {str(row.get("question_id")): row for row in rows if not _is_twin(row)}


def _has_original(twin: dict, originals: dict[str, dict]) -> bool:
    return longmemeval_data.original_of(str(twin.get("question_id"))) in originals


def _pair_figures(twins: list[dict], originals: dict[str, dict]) -> dict:
    paired = [twin for twin in twins if _has_original(twin, originals)]
    right = [twin for twin in paired if _pair_is_right(twin, originals)]
    return {"pairs": len(paired), "pairs_right": len(right)}


def twin_calibration(rows: list[dict]) -> dict:
    twins = [row for row in rows if _is_twin(row)]
    return {
        "twins": len(twins),
        "refused_when_twin": _count(twins, declined_to_answer),
        "answered_when_twin": _count(twins, _answered),
        **_pair_figures(twins, _originals(rows)),
    }


def split_calibration(rows: list[dict]) -> dict:
    """The calibration on each half of the stand, so the decide half is read and never tuned on."""
    halves: dict[str, list[dict]] = {longmemeval_data.TUNE: [], longmemeval_data.DECIDE: []}
    for row in rows:
        halves[longmemeval_data.split_of(str(row.get("question_id")))].append(row)
    return {half: abstention_calibration(members) for half, members in halves.items()}


def _category_report(rows: list[dict]) -> dict:
    """One category's numbers, with provider failures held out of accuracy.

    A question the provider never answered is not a wrong answer: nothing was
    produced to compare against the gold. Counting it as wrong would report a
    property of this machine's single `claude` CLI as a property of the memory
    system. So `n` is every attempt, `scored` is the attempts that produced an
    answer or an abstention, `accuracy` / `em` / `f1` are means over `scored`
    alone, and `provider_failures` carries the rest as its own line.
    """
    scored = [row for row in rows if not is_ungraded(row)]
    return {
        "n": len(rows),
        "scored": len(scored),
        **_quality_metrics(scored),
        **_prompt_evidence(scored),
        **_abstention_split(scored),
        "provider_failures": _count(rows, is_provider_failure),
        "harness_failures": _count(rows, is_harness_failure),
        **_cost_metrics(rows),
    }


def aggregate(rows: list[dict]) -> dict:
    """Per-category and overall report over scored per-question rows."""
    scored = [score_question(row) for row in rows]
    categories: dict[str, list[dict]] = {}
    for row in scored:
        categories.setdefault(str(row.get("category", "unknown")), []).append(row)
    report = {name: _category_report(members) for name, members in sorted(categories.items())}
    report["overall"] = _category_report(scored)
    return report
