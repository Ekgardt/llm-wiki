"""Exact original prompt bytes and existing parser edge behavior."""
import json
from pathlib import Path

import pytest
from llm_wiki import classification_protocol as protocol

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/classification-protocol-context.json')
BASELINE = json.loads(read_source(Path(__file__).with_name('prompt_baseline.json')))


def test_system_prompt_is_byte_identical():
    assert protocol.CLASSIFICATION_SYSTEM_PROMPT.encode() == BASELINE['system_prompt'].encode()


@pytest.mark.parametrize('case', BASELINE['prompts'])
def test_prompt_renderer_preserves_entire_original_bytes(case):
    actual = protocol.build_classification_prompt(case['transcript'], case['event'])
    assert actual.encode() == case['prompt'].encode()


@pytest.mark.parametrize('raw, expected', [
    (None, ('ok', '')),
    ('FLUSH_OK\nbody', ('ok', 'body')),
    ('``FLUSH_MAJOR``\n*hello*', ('major', 'hello')),
    ('(no durable content)', ('ok', '')),
    ('prefix\nNO_DURABLE_CONTENT\npost', ('ok', '')),
    ('{"operations": []}', ('ok', '')),
    ('{"audit": []}', ('ok', '')),
    ('{"other": 1}', ('minor', '{"other": 1}')),
    (' FLUSH_MINOR.\n\n body ', ('minor', 'body')),
    ('**FLUSH_MAJOR**\nsummary', ('minor', '**FLUSH_MAJOR**\nsummary')),
])
def test_existing_decorations_legacy_and_fallback_behavior(raw, expected):
    assert protocol.classify_response(raw) == expected
