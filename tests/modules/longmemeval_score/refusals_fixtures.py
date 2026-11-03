"""Single-owner synthetic metric fixtures shared by integrations."""
from __future__ import annotations


def _row(**fields: object) -> dict:
    return {
        "question_id": "q",
        "category": "multi-session",
        "question_type": "multi-session",
        "gold": "the paid tier",
        "hypothesis": "",
        "status": "insufficient_evidence",
        **fields,
    }


# The loss we could win: the reader declined a question whose every labelled
# evidence turn, and the gold string itself, were in the prompt it received.
_REFUSED_WITH_EVIDENCE = _row(
    gold_in_prompt=True,
    evidence_turns_labelled=2,
    evidence_in_prompt=True,
)


# Retrieval did the refusing: a prompt was built and the evidence was not in it.
_REFUSED_WITHOUT_EVIDENCE = _row(
    question_id="q2",
    gold_in_prompt=False,
    evidence_turns_labelled=2,
    evidence_in_prompt=False,
)


# The win nobody else measures: the gold is an explanation of why nothing can be
# answered, and the system was silent.
_CORRECT_SILENCE = _row(
    question_id="q3",
    category="abstention",
    question_type="abstention",
    is_abstention=True,
    gold="No session says which laptop was bought.",
)


# The same decision failing the other way: it answered where silence was right.
_INVENTED_ANSWER = _row(
    question_id="q4",
    category="abstention",
    question_type="abstention",
    is_abstention=True,
    gold="No session says which laptop was bought.",
    status="answered",
    hypothesis="You bought the fourteen-inch one.",
)
