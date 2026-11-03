"""The pilot declarations match the real, fully included provider source."""
from pathlib import Path

from tests.module_context import context_reader
from tests.module_contract_surface import require_declared_surface

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/contract-identity-context.json')


def test_answer_budget_interface_matches_all_declared_public_members():
    exports = require_declared_surface(
        read_source(ROOT / 'scripts/answer_budget.py'),
        read_source(ROOT / 'docs/modules/answer-budget-interface.pyi'),
    )
    assert exports == {
        'BYTES_PER_TOKEN', 'DEFAULT_BUDGET_TOKENS', 'DERIVABLE_FIELDS',
        'MAX_BUDGET_TOKENS', 'MIN_BUDGET_TOKENS', 'PROTECTED_FIELDS',
        'REPORT_TOKEN_ALLOWANCE', 'estimate_text_tokens', 'estimate_tokens',
        'shape_code_answer',
    }
