"""Legacy imports keep one implementation and resolve historical type names."""
import pickle

import ledger
import pytest
from llm_wiki import ledger_core


@pytest.mark.parametrize('name', ['Record', 'Pointer', 'LedgerCount', 'Reconciliation', 'Recurrence'])
def test_old_pickle_global_resolves_the_identical_core_type(name):
    # Trusted constant protocol-0 GLOBAL, not an external serialized input.
    historical = f'cledger\n{name}\n.'.encode('ascii')
    assert pickle.loads(historical) is getattr(ledger_core, name)
    assert getattr(ledger, name) is getattr(ledger_core, name)


@pytest.mark.parametrize('name', [
    'records_of', 'ensure_table', 'post', 'rows', 'rows_in_store', 'write_table',
    'count', 'sum_quantities', 'reconcile', 'recurring', 'entity_page_bytes',
    'canonical', 'canonical_kind', 'same_event',
])
def test_public_entry_exports_the_real_core_function(name):
    assert getattr(ledger, name) is getattr(ledger_core, name)
