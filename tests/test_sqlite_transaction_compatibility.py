"""Historical imports and serialized function identities remain compatible."""
import ast
import base64
import json
import pickle
from pathlib import Path

import reliable_memory
from llm_wiki.sqlite_transactions import begin_immediate

ROOT = Path(__file__).resolve().parents[1]


def test_old_import_and_historical_pickle_are_identical():
    record = json.loads((ROOT / 'tests/modules/sqlite_transactions/fixtures/function-pickle.json').read_text(encoding='utf-8'))
    assert reliable_memory.begin_immediate is begin_immediate
    encoded = base64.b64decode(record['pickle'])
    assert pickle.loads(encoded) is begin_immediate
    assert pickle.dumps(begin_immediate, protocol=4) == encoded
    assert begin_immediate.__wrapped__.__module__ == record['wrapped_module']


def _old_transaction_imports(path):
    nodes = _from_imports(path)
    old = [n for n in nodes if n.module == 'reliable_memory']
    return any(a.name == 'begin_immediate' for n in old for a in n.names)


def test_production_consumers_use_transaction_owner_directly():
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / 'scripts').rglob('*.py') if _old_transaction_imports(p)]
    assert not offenders


def _from_imports(path):
    return [n for n in ast.walk(ast.parse(path.read_text(encoding='utf-8'))) if isinstance(n, ast.ImportFrom)]
