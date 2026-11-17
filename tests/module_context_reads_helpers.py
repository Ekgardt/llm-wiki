"""Helpers shared by test_module_context_reads.py and the test files importing them."""

import ast


def _unchecked_reads(source):
    calls = [n.func for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)]
    names = {getattr(n, 'attr', getattr(n, 'id', '')) for n in calls}
    return names & {'read_text', 'read_bytes', 'open'}
