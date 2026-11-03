"""Experimental declared API; requires full answer-budget-semantics.md.

Not a runtime replacement or a qualified source-closure exemption.
"""
from __future__ import annotations

MAX_BUDGET_TOKENS = 25000

MIN_BUDGET_TOKENS = 32

DEFAULT_BUDGET_TOKENS = MAX_BUDGET_TOKENS

DERIVABLE_FIELDS = frozenset({'owner'})

PROTECTED_FIELDS = frozenset({'path', 'file', 'line', 'name', 'function', 'qualified_name', 'symbol', 'status', 'error', 'mode', 'directory', 'source_generation', 'graph_complete', 'unresolved_count', 'fallback', 'frontier_truncated', 'refused_expansions', 'refused_node_ids'})

REPORT_TOKEN_ALLOWANCE = 96

BYTES_PER_TOKEN = 2

def estimate_text_tokens(text: str) -> int:
    ...

def estimate_tokens(data) -> int:
    ...

def shape_code_answer(data, *, budget_tokens: int | None=None, include_node_ids: bool=False):
    ...
