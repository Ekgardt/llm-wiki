"""Helpers shared by test_the_offline_ledger_reads_the_question_not_the_label.py and the test files importing them."""
from __future__ import annotations

QUESTION = {
    "question": "How many bikes did I service in March?",
    "question_date": "2023/03/20 (Mon) 23:57",
    "haystack_dates": ["2023/03/20 (Mon) 07:32", "2023/03/20 (Mon) 10:35"],
    "haystack_sessions": [
        [
            {"role": "user", "content": "I need a new tire for my commuter bike this month."},
            {"role": "assistant", "content": "A bike shop can fit one."},
        ],
        [{"role": "user", "content": "My road bike runs great. I own two bikes now."}],
    ],
}
