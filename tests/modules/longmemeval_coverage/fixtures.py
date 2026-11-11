"""Shared original synthetic evidence for coverage and worker integration."""

_EVIDENCE = "the user upgraded to the paid tier of the editing suite on a friday"

_OTHER = "the user asked about which microphone to buy for a podcast"

_QUESTION = {
    "answer_session_ids": ["s1"],
    "haystack_sessions": [
        [
            {"role": "user", "content": _EVIDENCE, "has_answer": True},
            {"role": "user", "content": _OTHER},
        ]
    ],
}
