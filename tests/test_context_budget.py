from __future__ import annotations

import ast
import json
import math
import sys
from pathlib import Path

import pytest
from context_budget import (
    ContextBudget,
    TokenCount,
    TokenUsage,
    count_tokens,
)


def test_context_budget_is_immutable_and_exposes_available_input():
    budget = ContextBudget("model", 100, 20, 5)

    assert budget.available_input_tokens == 75
    with pytest.raises(AttributeError):
        budget.max_input_tokens = 200


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (("model", True, 0, 0), "max_input_tokens"),
        (("model", 0, 0, 0), "max_input_tokens"),
        (("model", 10, True, 0), "reserved_output_tokens"),
        (("model", 10, -1, 0), "reserved_output_tokens"),
        (("model", 10, 0, True), "safety_margin_tokens"),
        (("model", 10, 0, -1), "safety_margin_tokens"),
    ],
)
def test_context_budget_rejects_invalid_integer_fields(values, message):
    with pytest.raises(ValueError, match=message):
        ContextBudget(*values)


@pytest.mark.parametrize("reserved,margin", [(10, 0), (5, 5), (9, 2)])
def test_context_budget_rejects_impossible_budget(reserved, margin):
    with pytest.raises(ValueError, match="available input"):
        ContextBudget("model", 10, reserved, margin)


def test_token_usage_defaults_are_unknown_and_immutable():
    usage = TokenUsage()

    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.cache_read_tokens is None
    assert usage.cache_write_tokens is None
    assert usage.duration_ms is None
    assert usage.estimated_cost is None
    assert usage.cost_kind == "unknown"
    with pytest.raises(AttributeError):
        usage.input_tokens = 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"input_tokens": True}, "input_tokens"),
        ({"output_tokens": -1}, "output_tokens"),
        ({"cache_read_tokens": 1.5}, "cache_read_tokens"),
        ({"cache_write_tokens": False}, "cache_write_tokens"),
        ({"duration_ms": -1}, "duration_ms"),
        ({"estimated_cost": True, "cost_kind": "reported"}, "estimated_cost"),
        ({"estimated_cost": -0.1, "cost_kind": "estimated"}, "estimated_cost"),
        ({"estimated_cost": math.inf, "cost_kind": "estimated"}, "estimated_cost"),
        ({"estimated_cost": math.nan, "cost_kind": "estimated"}, "estimated_cost"),
        ({"cost_kind": "reported"}, "cost_kind"),
        ({"estimated_cost": 0.1}, "cost_kind"),
    ],
)
def test_token_usage_rejects_invalid_or_incoherent_values(kwargs, message):
    with pytest.raises(ValueError, match=message):
        TokenUsage(**kwargs)


@pytest.mark.parametrize("kind", ["reported", "estimated"])
def test_token_usage_accepts_explicit_monetary_cost_kind(kind):
    assert TokenUsage(estimated_cost=0.0, cost_kind=kind).cost_kind == kind


def test_registered_model_adapter_is_labeled_tokenizer():
    result = count_tokens("hello", model="known", adapters={"known": lambda text: 3})

    assert result == TokenCount(tokens=3, source="tokenizer")


def test_reported_count_retains_its_source_label():
    assert TokenCount(tokens=7, source="reported").source == "reported"


@pytest.mark.parametrize(
    "adapter",
    [lambda text: True, lambda text: -1, lambda text: 1.5, lambda text: (_ for _ in ()).throw(RuntimeError())],
)
def test_invalid_adapter_fails_without_an_exact_claim(adapter):
    result = count_tokens("hello", model="known", adapters={"known": adapter})

    assert result == TokenCount(tokens=None, source="unknown")


def test_unknown_unicode_text_uses_conservative_utf8_estimate():
    text = "A\N{EURO SIGN}\N{GRINNING FACE}"

    result = count_tokens(text, model="unknown")

    assert result == TokenCount(tokens=len(text.encode("utf-8")), source="estimated")


@pytest.mark.parametrize("text", ["\ud800", "prefix\udfff suffix"])
def test_unpaired_surrogate_returns_unknown_instead_of_raising(text):
    assert count_tokens(text) == TokenCount(tokens=None, source="unknown")


def test_empty_input_has_deterministic_zero_estimate():
    assert count_tokens("") == TokenCount(tokens=0, source="estimated")




_ROOT = Path(__file__).resolve().parents[1]


def _import_roots(node):
    if isinstance(node, ast.Import):
        return {alias.name.split('.')[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return {('.' * node.level + (node.module or '')).split('.')[0]}
    return set()


def _nonstdlib_imports(source):
    roots = set().union(*(_import_roots(node) for node in ast.walk(ast.parse(source))))
    return roots - sys.stdlib_module_names


def test_context_budget_has_no_execution_dependencies():
    source = (_ROOT / 'scripts/context_budget.py').read_text(encoding='utf-8')
    assert not _nonstdlib_imports(source)


@pytest.mark.parametrize('source', [
    'import memory_state',
    'from llm_client import call_llm',
    'def count():\n import compile_memory as executor',
    'from reliable_memory import *',
    'from . import local',
    'from .json import loads',
])
def test_budget_boundary_detects_nonstandard_dependencies(source):
    assert _nonstdlib_imports(source)


def test_budget_boundary_accepts_standard_library_imports():
    assert not _nonstdlib_imports('import json as wire\nfrom dataclasses import dataclass')


def test_complete_context_budget_module_fits_user_ceiling():
    manifest = json.loads((_ROOT / 'docs/modules/context-budget-context.json').read_text())
    paths = manifest['files']
    assert {'scripts/context_budget.py', 'tests/test_context_budget.py',
            'tests/test_context_packing.py', 'docs/modules/context-budget.md',
            'docs/modules/context-budget-context.json'} <= set(paths)
    assert len(paths) == len(set(paths))
    assert manifest['max_tokens'] == 100_000
    from tests.module_context import measure_context
    total, files = measure_context(_ROOT, _ROOT / 'docs/modules/context-budget-context.json')
    print(f'context-budget: {total} tokens; {files} files')
