"""Helpers shared by test_beam_becomes_a_question_this_stand_can_read.py and the test files importing them."""




CONVERSATION = {
    "conversation_id": "7",
    "chat": [
        [
            {"role": "user", "content": "First thing I said. ->-> 1,1", "time_anchor": "March-15-2024"},
            {"role": "assistant", "content": "First reply. ->-> 1,2", "time_anchor": "None"},
        ],
        [
            {"role": "user", "content": "Later thing. ->-> 2,1", "time_anchor": "April-05-2024"},
        ],
    ],
    "probing_questions": str(
        {
            "abstention": [
                {
                    "question": "What did the review say?",
                    "ideal_response": "Based on the provided chat, there is no information.",
                    "why_unanswerable": "never discussed",
                    "difficulty": "medium",
                }
            ],
            "temporal_reasoning": [
                {"question": "When did it start?", "answer": "15 March 2024"}
            ],
            "summarization": [
                {"question": "Summarise it", "ideal_summary": "a summary"}
            ],
            "instruction_following": [
                {"question": "Did I comply?", "expected_compliance": "yes"}
            ],
        }
    ),
}
