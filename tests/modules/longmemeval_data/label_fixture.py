"""Shared synthetic question; no execution dependencies."""

def _locomo_question(**fields: object) -> dict:
    return {
        "question_id": "conv-26_7",
        "question_type": "single-hop",
        "question": "What did Melanie buy?",
        "answer": "new shoes",
        "question_date": "2023/06/01 (Thu) 12:00",
        "haystack_session_ids": ["session_1", "session_2"],
        "haystack_dates": ["2023/05/20 (Sat) 02:21", "2023/05/22 (Mon) 09:00"],
        "haystack_sessions": [
            [{"role": "user", "content": "Hi there."}, {"role": "assistant", "content": "Hello."}],
            [{"role": "user", "content": "I ran today."}, {"role": "assistant", "content": "I bought new shoes."}],
        ],
        "answer_session_ids": [],
        "locomo_evidence": ["D2:2"],
        **fields,
    }
