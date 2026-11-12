"""Existing journal facade and pickle paths retain exact event identities."""
import base64
import json
import pickle
from pathlib import Path

import compile_progress as facade
from llm_wiki import compile_event_records as records

ORIGINAL = json.loads((Path(__file__).parent / 'modules/compile_event_records/legacy-event.json').read_text(encoding='utf-8'))


def test_existing_facade_exports_the_same_event_classes():
    for name in ('CompileEvent', 'ProgressConflict', 'ProgressCorrupt', 'SourceOutcomeConflict'):
        assert getattr(facade, name) is getattr(records, name)
        assert getattr(facade, name).__module__ == 'compile_progress'


def test_historical_event_pickle_decodes_and_reencodes_exactly():
    raw = base64.b64decode(ORIGINAL['pickle_protocol_4'])
    event = pickle.loads(raw)
    assert type(event) is records.CompileEvent
    assert event == records.CompileEvent(**ORIGINAL['event'])
    assert pickle.dumps(event, protocol=4) == raw


def test_storage_exports_the_same_validator_functions():
    names = ('canonical', 'event_digest', 'require_digest', 'require_sequence',
             'require_labels', 'require_details', 'validate_event',
             'snapshot_event', 'artifact_identity', 'require_model_phase')
    for name in names:
        assert getattr(facade, name) is getattr(records, name)
