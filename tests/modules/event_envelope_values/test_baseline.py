"""Whole original encoded values and public signature survive extraction."""
import inspect
import json
from datetime import datetime
from pathlib import Path

from llm_wiki import event_envelope_values as values

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/event-envelope-values-context.json')


def test_original_complete_json_and_signature():
    baseline = json.loads(read_source(Path(__file__).with_name('baseline.json')))
    moment = datetime.fromisoformat('2026-07-13T14:30:45+02:00')
    encoded = [values.build_event_envelope(
        event_type='user_prompt', payload=payload, occurred_at=moment, captured_at=moment,
        agent='codex', source_event_id='host-1',
    ).to_json() for payload in baseline['input_payloads']]
    assert encoded == baseline['complete_json']
    assert str(inspect.signature(values.build_event_envelope)) == baseline['signature']
    assert values.EventEnvelope.__module__ == baseline['class_module']
