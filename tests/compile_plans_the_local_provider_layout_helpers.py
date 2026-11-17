"""Helpers shared by test_compile_plans_the_local_provider_layout.py and the test files importing them."""




def _adversarial_count(text):
    return 1 if text.startswith("SYSTEM:") else 10000


def _byte_count(text):
    return len(text.encode())
