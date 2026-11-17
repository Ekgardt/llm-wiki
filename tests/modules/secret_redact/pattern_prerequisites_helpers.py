"""Helpers shared by test_pattern_prerequisites.py and the test files importing them."""

import secret_redact as redactor


def original_patterns(text):
    for pattern, replacement in redactor.PATTERNS:
        text = pattern.sub(replacement, text)
    return text
