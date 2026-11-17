"""Synthetic paired rows shared by pure statistics and runner tests."""
from __future__ import annotations


def paired(question_id: str, baseline: dict, consolidated: dict, **extra) -> dict:
    """A paired row in the shape the worker writes."""
    return {
        "question_id": question_id,
        "question_type": "multi-session",
        "category": "multi-session",
        "is_abstention": False,
        "question": "how many people did the campaign reach?",
        "gold": "4200",
        "baseline": baseline,
        "consolidated": consolidated,
        **extra,
    }


def answered(text: str, **extra) -> dict:
    return {"status": "answered", "hypothesis": text, "error": None, **extra}


def failed(kind: str = "provider_no_response") -> dict:
    return {"status": "error", "hypothesis": "", "error": "boom", "error_kind": kind}
