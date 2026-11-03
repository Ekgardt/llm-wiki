"""Cross-owner aliases and serialization are integration qualification."""
import ast
import base64
import json
import pickle
from pathlib import Path

import bounded_io
import memory_queue
import reliable_memory
from llm_wiki import path_ancestry, queue_artifacts, sqlite_io

ROOT = Path(__file__).resolve().parents[1]


def _definitions(path):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}


def test_legacy_names_are_identical_and_have_no_second_implementation():
    names = _definitions('scripts/llm_wiki/queue_artifacts.py')
    definitions = _definitions('scripts/memory_queue.py')
    assert not names & definitions
    for name in names:
        assert getattr(memory_queue, name) is getattr(queue_artifacts, name)


def test_legacy_aliases_are_the_only_reader_implementation():
    tree = ast.parse((ROOT / 'scripts/bounded_io.py').read_text(encoding='utf-8'))
    definitions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ('_acceptable_ancestor', '_system_symlink', '_root_controlled'):
        assert name not in definitions
        assert getattr(bounded_io, name) is getattr(path_ancestry, name)


def test_public_aliases_preserve_pickle_bytes():
    fixture = ROOT / 'tests/modules/sqlite_io/legacy-pickle.json'
    for name, encoded in json.loads(fixture.read_text(encoding='utf-8')).items():
        value = getattr(sqlite_io, name)
        assert getattr(reliable_memory, name) is value
        assert pickle.dumps(value, protocol=4) == base64.b64decode(encoded)
        assert pickle.loads(base64.b64decode(encoded)) is value


def test_public_pem_helper_keeps_identity_and_serialization():
    import lsp_security
    from llm_wiki import lsp_credentials
    helper = lsp_security.redact_private_key_blocks
    assert helper is lsp_credentials.redact_private_key_blocks
    assert helper.__module__ == 'lsp_security'
    assert pickle.loads(pickle.dumps(helper)) is helper


def test_queue_record_aliases_and_internal_row_pickle_stay_compatible():
    from llm_wiki import queue_task_records

    for name in ('_task_record', '_export_task_record', '_exported_task_record', '_ExportedRows', '_attempt_history_entry'):
        assert getattr(memory_queue, name) is getattr(queue_task_records, name)
    rows = memory_queue._ExportedRows([], [], None, None)
    assert type(rows).__module__ == 'memory_queue'
    restored = pickle.loads(pickle.dumps(rows, protocol=4))
    assert type(restored) is memory_queue._ExportedRows
    assert restored == rows


def test_queue_evidence_aliases_and_serialized_identity_are_retained():
    import pickle

    import memory_queue
    from llm_wiki import queue_evidence_records

    names = ('_CorruptEvidence', '_corrupt_metadata', '_corrupt_disposition_key',
             '_corrupt_records_match', '_corrupt_manifest', '_corrupt_disposition_record',
             '_capture_resolution_record', '_purge_receipt_record', '_capture_seal_digest',
             '_require_identical_seal')
    for name in names:
        assert getattr(memory_queue, name) is getattr(queue_evidence_records, name)
    value = queue_evidence_records._CorruptEvidence(b'a', b'b', b'c', 'd', 'e', 'f', 'g', 1)
    assert pickle.loads(pickle.dumps(value)) == value


def _record_fixture(module):
    path = ROOT / f'tests/modules/{module}/original-contract.json'
    return json.loads(path.read_text(encoding='utf-8'))


def _assert_record_serialization(module, facade):
    import importlib

    records = importlib.import_module(f'llm_wiki.{module}')
    legacy = importlib.import_module(facade)
    fixture = _record_fixture(module)
    for name, encoded in fixture['classes'].items():
        value = getattr(records, name)
        assert getattr(legacy, name) is value
        assert pickle.loads(base64.b64decode(encoded)) is value
        assert pickle.dumps(value, protocol=4) == base64.b64decode(encoded)
    for name, expected in fixture['errors'].items():
        value = pickle.loads(base64.b64decode(expected['pickle']))
        assert type(value) is getattr(records, name)
        assert str(value) == expected['message']
        assert json.loads(json.dumps(vars(value))) == expected['attributes']
        assert pickle.dumps(value, protocol=4) == base64.b64decode(expected['pickle'])


def test_ownership_records_retain_old_serialization():
    _assert_record_serialization('ownership_contracts', 'operational_ownership')


def test_markdown_records_retain_old_serialization():
    _assert_record_serialization('markdown_records', 'markdown_transaction')


def test_ownership_validation_aliases_keep_one_serializable_implementation():
    import operational_ownership
    from llm_wiki import ownership_validation

    names = _definitions('scripts/llm_wiki/ownership_validation.py')
    assert not names & _definitions('scripts/operational_ownership.py')
    for name in names:
        value = getattr(ownership_validation, name)
        assert getattr(operational_ownership, name) is value
        assert pickle.loads(pickle.dumps(value, protocol=4)) is value
