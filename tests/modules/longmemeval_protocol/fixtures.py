"""One synthetic row shared by protocol and judge tests."""

def _row(**overrides) -> dict:
    row = {
        "question_id": "q1",
        "question_type": "single-session-user",
        "category": "single-session-user",
        "question": "What colour is my car?",
        "gold": "blue",
        "hypothesis": "Your car is blue.",
        "status": "answered",
        "reason": None,
    }
    row.update(overrides)
    return row
