"""Golden event bytes and rejection behavior are independent of journal IO."""
import json
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path

import pytest
from llm_wiki import compile_event_records as records

ORIGINAL = json.loads((Path(__file__).parent / 'legacy-event.json').read_text(encoding='utf-8'))


def _event():
    return records.CompileEvent(**json.loads(json.dumps(ORIGINAL['event'])))


def test_canonical_bytes_digest_and_snapshot_match_original():
    event = _event()
    assert records._canonical(asdict(event)) == ORIGINAL['canonical']
    assert records._event_digest(event) == ORIGINAL['digest']
    assert records._snapshot_event(event) == event
    assert type(event).__module__ == ORIGINAL['class_module']


def test_snapshot_isolated_from_mutable_details_and_event_is_frozen():
    event = _event()
    snapshot = records._snapshot_event(event)
    event.details['items'].append('later')
    assert snapshot.details['items'] == [1, True, None]
    with pytest.raises(FrozenInstanceError):
        event.sequence = 9


@pytest.mark.parametrize('changes', [
    {'work_id': 'A' * 64}, {'source_digest': 'invalid'}, {'sequence': True},
    {'sequence': -1}, {'stage': 'unknown'}, {'outcome': 'unknown'},
    {'task_id': ''}, {'task_id': 1}, {'details': []},
    {'details': {'tuple': (1, 2)}}, {'details': {'number': float('nan')}},
    {'details': {'unsupported': {1}}},
])
def test_invalid_or_lossy_event_is_refused(changes):
    with pytest.raises((ValueError, TypeError)):
        records._snapshot_event(replace(_event(), **changes))


def test_artifact_identity_matches_existing_metadata():
    event = _event()
    artifact = event.details['artifact']
    assert records.artifact_identity(event) == (artifact['reference'], artifact['sha256'])


@pytest.mark.parametrize('artifact', [
    {}, None, {'reference': '../outside', 'sha256': 'd' * 64},
    {'reference': 'run/queue-results/' + 'c' * 64 + '.result', 'sha256': 'D' * 64},
    {'reference': 'run/queue-results/' + 'c' * 64 + '.result/extra', 'sha256': 'd' * 64},
])
def test_untrusted_artifact_metadata_is_refused(artifact):
    with pytest.raises(records.ProgressCorrupt):
        records.artifact_identity(replace(_event(), details={'artifact': artifact}))


@pytest.mark.parametrize('phase', ['dispatch', 'answer'])
def test_valid_model_phase_is_unchanged(phase):
    records._require_model_phase(replace(_event(), details={'model_call': {'phase': phase}}))


@pytest.mark.parametrize('call', [[], None, {}, {'phase': 'finished'}])
def test_invalid_model_phase_is_corrupt(call):
    with pytest.raises(records.ProgressCorrupt):
        records._require_model_phase(replace(_event(), details={'model_call': call}))


def test_source_conflict_retains_identity_and_exception_family():
    event = _event()
    error = records.SourceOutcomeConflict(event)
    assert isinstance(error, records.ProgressConflict)
    assert (error.work_id, error.sequence) == (event.work_id, event.sequence)
    assert str(error) == f'source outcome unresolved in work {event.work_id} at {event.sequence}'
