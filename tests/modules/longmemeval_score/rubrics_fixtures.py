"""Single-owner synthetic metric fixtures shared by integrations."""
from __future__ import annotations

_RUBRIC = {
    "question_id": "pref1",
    "category": "single-session-preference",
    "question_type": "single-session-preference",
    "gold": "The user would prefer responses tailored to Adobe Premiere Pro.",
    "hypothesis": "Try the advanced warp-stabiliser settings you already use.",
    "status": "answered",
}


_SPAN = {
    "question_id": "fact1",
    "category": "multi-session",
    "question_type": "multi-session",
    "gold": "25:50",
    "hypothesis": "Your personal best was 25:50.",
    "status": "answered",
}


_TEXT_KEYS = ("em", "contains", "f1", "correct")
