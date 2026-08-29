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
from collections import Counter

_ARTICLES = frozenset({"a", "an", "the"})
_NON_ALNUM = re.compile(r"[^0-9a-zЀ-ӿ]+")

PROVIDER_ERROR_KINDS = frozenset(
    {"provider_no_response", "provider_invalid_json", "provider_deadline"}
)


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


def score_question(result: dict) -> dict:
    """Attach deterministic metrics to one per-question result record."""
    if result.get("is_abstention"):
        return _scored_abstention(result)
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


def _metric_values(rows: list[dict], key: str) -> list[float]:
    return [float(row[key]) for row in rows if row.get(key) is not None]


def is_provider_failure(row: dict) -> bool:
    """The provider never answered, so the memory system was never graded."""
    return row.get("error_kind") in PROVIDER_ERROR_KINDS


def _flags(rows: list[dict], key: str) -> list[float]:
    return [float(bool(row.get(key))) for row in rows]


def _quality_metrics(scored: list[dict]) -> dict:
    return {
        "accuracy": _mean(_flags(scored, "correct")),
        "em": _mean(_flags(scored, "em")),
        "f1": _mean(_metric_values(scored, "f1")),
    }


def _cost_metrics(rows: list[dict]) -> dict:
    return {
        "mean_prompt_chars": _mean(_metric_values(rows, "prompt_chars")),
        "mean_est_prompt_tokens": _mean(_metric_values(rows, "est_prompt_tokens")),
        "mean_retrieve_seconds": _mean(_metric_values(rows, "retrieve_seconds")),
        "mean_answer_seconds": _mean(_metric_values(rows, "answer_seconds")),
        "mean_total_seconds": _mean(_metric_values(rows, "total_seconds")),
    }


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


def _category_report(rows: list[dict]) -> dict:
    """One category's numbers, with provider failures held out of accuracy.

    A question the provider never answered is not a wrong answer: nothing was
    produced to compare against the gold. Counting it as wrong would report a
    property of this machine's single `claude` CLI as a property of the memory
    system. So `n` is every attempt, `scored` is the attempts that produced an
    answer or an abstention, `accuracy` / `em` / `f1` are means over `scored`
    alone, and `provider_failures` carries the rest as its own line.
    """
    scored = [row for row in rows if not is_provider_failure(row)]
    return {
        "n": len(rows),
        "scored": len(scored),
        **_quality_metrics(scored),
        **_abstention_split(scored),
        "provider_failures": len(rows) - len(scored),
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
