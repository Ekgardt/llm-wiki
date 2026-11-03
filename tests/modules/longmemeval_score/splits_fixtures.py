"""Single-owner synthetic metric fixtures shared by integrations."""
from __future__ import annotations


def _row(**fields: object) -> dict:
    return {"question_id": "q1", "category": "single-session-user", "gold": "a gravel bike", **fields}
