"""Shared synthetic question; no execution dependencies."""

def _question(**fields: object) -> dict:
    return {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "Which bike did I buy?",
        "answer": "a gravel bike",
        "question_date": "2023/05/30 (Tue) 20:42",
        "haystack_session_ids": ["s1", "s2", "s3"],
        "haystack_dates": ["2023/05/20 (Sat) 02:21", "2023/05/22 (Mon) 09:00", "2023/05/25 (Thu) 18:00"],
        "haystack_sessions": [
            [{"role": "user", "content": "I bought a gravel bike.", "has_answer": True}],
            [{"role": "user", "content": "The weather was fine."}],
            [{"role": "user", "content": "My commuter bike needs a chain."}],
        ],
        "answer_session_ids": ["s1"],
        **fields,
    }
