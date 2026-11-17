"""The queue facade's legacy names are the artifact, task-record and evidence-record owners' objects, with no second implementation, and their rows and evidence keep their pickle identity."""

import ast
import pickle

import queue_core  # noqa: E402
from llm_wiki import queue_artifacts

from tests.module_legacy_alias_helpers import (
    _definitions,
)
from tests.queue_engine_sources import queue_engine_tree


def test_legacy_names_are_identical_and_have_no_second_implementation():
    names = _definitions('scripts/llm_wiki/queue_artifacts.py')
    definitions = {n.name for n in queue_engine_tree().body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert not names & definitions
    retained = names & vars(queue_core).keys()
    assert retained
    for name in retained:
        assert getattr(queue_core, name) is getattr(queue_artifacts, name)


def test_queue_record_aliases_and_internal_row_pickle_stay_compatible():
    from llm_wiki import queue_task_records

    for name in ('task_record', 'export_task_record', 'exported_task_record', 'ExportedRows', 'attempt_history_entry'):
        assert getattr(queue_core, name, getattr(queue_task_records, name)) is getattr(queue_task_records, name)
    rows = queue_core.ExportedRows([], [], None, None)
    assert type(rows).__module__ == 'memory_queue'
    restored = pickle.loads(pickle.dumps(rows, protocol=4))
    assert type(restored) is queue_core.ExportedRows
    assert restored == rows


def test_queue_evidence_aliases_and_serialized_identity_are_retained():
    import pickle

    from llm_wiki import queue_evidence_records

    names = ('CorruptEvidence', 'corrupt_metadata', 'corrupt_disposition_key',
             'corrupt_records_match', 'corrupt_manifest', 'corrupt_disposition_record',
             'capture_resolution_record', 'purge_receipt_record', 'capture_seal_digest',
             'require_identical_seal')
    for name in names:
        assert getattr(queue_core, name, getattr(queue_evidence_records, name)) is getattr(queue_evidence_records, name)
    value = queue_evidence_records.CorruptEvidence(b'a', b'b', b'c', 'd', 'e', 'f', 'g', 1)
    assert pickle.loads(pickle.dumps(value)) == value
