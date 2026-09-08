"""LongMemEval's own grading protocol, so our number can stand beside theirs.

Every published LongMemEval figure — Mem0 0.944, Supermemory 0.95, OMEGA
0.954, Mastra 0.949, Zep 0.712 — comes from the authors' `evaluate_qa.py`:
one yes/no question to a judge per hypothesis, a template per question type,
the abstention questions graded on whether the model said it could not
answer, `'yes' in reply.lower()` as the label, temperature 0, ten tokens.
Our judge asked a different question with a different prompt, so the two
numbers were never the same metric.

The templates below are the authors' character for character
(https://github.com/xiaowu0162/LongMemEval, MIT, `src/evaluation/evaluate_qa.py`).
What still differs, and is written into every report: the authors grade with
GPT-4o, and this vault grades with the Claude subscription it already has;
`--protocol official` therefore names the judge beside the number. Anyone
holding an OpenAI key can grade the same answers with the real script — the
hypothesis file `longmemeval_hypotheses.py` writes is the format it reads.

Two overall numbers are reported, because the field publishes both without
saying which: the plain mean over every question, and the mean of the
per-type means (OMEGA's 95.4% is the second; its plain mean is 93.2%).
See `docs/research/2026-09-08-the-official-protocol-on-our-judge.md`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

FACT = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response is equivalent to the correct answer or contains "
    "all the intermediate steps to get the correct answer, you should also answer "
    "yes. If the response only contains a subset of the information required by "
    "the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel "
    "Response: {}\n\nIs the model response correct? Answer yes or no only."
)
TEMPORAL = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response is equivalent to the correct answer or contains "
    "all the intermediate steps to get the correct answer, you should also answer "
    "yes. If the response only contains a subset of the information required by "
    "the answer, answer no. In addition, do not penalize off-by-one errors for "
    "the number of days. If the question asks for the number of days/weeks/months, "
    "etc., and the model makes off-by-one errors (e.g., predicting 19 days when "
    "the answer is 18), the model's response is still correct. \n\nQuestion: "
    "{}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response "
    "correct? Answer yes or no only."
)
KNOWLEDGE_UPDATE = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response contains some previous information along with an "
    "updated answer, the response should be considered as correct as long as the "
    "updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: "
    "{}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
)
PREFERENCE = (
    "I will give you a question, a rubric for desired personalized response, and "
    "a response from a model. Please answer yes if the response satisfies the "
    "desired response. Otherwise, answer no. The model does not need to reflect "
    "all the points in the rubric. The response is correct as long as it recalls "
    "and utilizes the user's personal information correctly.\n\nQuestion: "
    "{}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? "
    "Answer yes or no only."
)
ABSTENTION = (
    "I will give you an unanswerable question, an explanation, and a response "
    "from a model. Please answer yes if the model correctly identifies the "
    "question as unanswerable. The model could say that the information is "
    "incomplete, or some other information is given but the asked information is "
    "not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the "
    "model correctly identify the question as unanswerable? Answer yes or no only."
)
TEMPLATES = {
    "single-session-user": FACT,
    "single-session-assistant": FACT,
    "multi-session": FACT,
    "temporal-reasoning": TEMPORAL,
    "knowledge-update": KNOWLEDGE_UPDATE,
    "single-session-preference": PREFERENCE,
}
MAX_JUDGE_TOKENS = 10
# What the system says when it does not answer. The authors' abstention
# template asks whether the model "correctly identifies the question as
# unanswerable"; a status code is not a sentence a judge can read, so the
# refusal is rendered the way the product renders it to a person.
SILENCE = "I don't have that information in our conversation history."


def is_abstention(row: Mapping[str, object]) -> bool:
    return "_abs" in str(row.get("question_id", "")) or bool(row.get("is_abstention"))


def official_hypothesis(row: Mapping[str, object]) -> str:
    """The text the judge reads: the answer, or the refusal as a sentence."""
    if row.get("status") == "answered":
        return str(row.get("hypothesis") or "")
    reason = str(row.get("reason") or "").strip()
    if not reason:
        return SILENCE
    return f"{SILENCE} {reason}"


def official_prompt(row: Mapping[str, object]) -> str:
    """The authors' prompt for this row, abstention decided by the id as they do."""
    question = str(row.get("question", ""))
    answer = str(row.get("gold", ""))
    response = official_hypothesis(row)
    if is_abstention(row):
        return ABSTENTION.format(question, answer, response)
    template = TEMPLATES.get(str(row.get("question_type") or row.get("category")))
    if template is None:
        raise ValueError(f"no official template for question type {row.get('question_type')!r}")
    return template.format(question, answer, response)


def official_label(reply: str | None) -> bool:
    """Exactly the authors' parse: the word yes anywhere in the lowered reply."""
    return "yes" in (reply or "").lower()


def _type_of(row: Mapping[str, object]) -> str:
    if is_abstention(row):
        return "abstention"
    return str(row.get("question_type") or row.get("category"))


def _labels_by_type(rows: Iterable[Mapping[str, object]]) -> dict[str, list[float]]:
    labels: dict[str, list[float]] = {}
    for row in rows:
        label = row.get("official_label")
        if isinstance(label, bool):
            labels.setdefault(_type_of(row), []).append(float(label))
    return labels


def official_accuracy(rows: Iterable[Mapping[str, object]]) -> dict:
    """Per-type accuracy, the plain mean, and the mean of the type means."""
    labels = _labels_by_type(rows)
    per_type = {name: _mean(values) for name, values in sorted(labels.items())}
    every = [value for values in labels.values() for value in values]
    return {
        "per_type": per_type,
        "n": len(every),
        "accuracy": _mean(every),
        "task_averaged": _mean(list(per_type.values())),
    }


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def hypothesis_lines(rows: Iterable[Mapping[str, object]]) -> list[str]:
    """The authors' hypothesis file: one `{question_id, hypothesis}` per line."""
    return [
        json.dumps(
            {"question_id": str(row.get("question_id")), "hypothesis": official_hypothesis(row)},
            ensure_ascii=False,
        )
        for row in rows
    ]
