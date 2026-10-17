"""The actual draft request cannot invite a nonexistent native source selector."""
import json

import compile_memory as compiler


def _inputs(projected=None, context_projected=None):
    path = 'knowledge/daily/2026-09-25.md'
    body = b'- A durable user fact.\n'
    daily = compiler.DailySnapshot(path, body, compiler.sha256_bytes(body))
    source = compiler.SourceSnapshot(path, body, compiler.sha256_bytes(body), prompt_content=projected)
    context = compiler.SourceSnapshot('knowledge/notes/context.md', b'context', 'context',
                                      prompt_content=context_projected)
    return compiler.CompileInputs((daily,), (source, context), ())


def _request_schema(monkeypatch, inputs):
    captured = []
    original = compiler.planning_input_text

    def recording(prompt, system, schema):
        captured.append(schema)
        return original(prompt, system, schema)

    monkeypatch.setattr(compiler, 'planning_input_text', recording)
    compiler._draft_prompt_text(inputs)
    return captured[0]['properties']['operations']['items']['properties']['evidence']['items']


def test_legacy_source_request_does_not_offer_native_event(monkeypatch):
    schema = _request_schema(monkeypatch, _inputs())
    assert 'oneOf' not in schema
    assert 'native_event' not in schema['properties']


def test_optional_context_does_not_grant_native_source_protocol(monkeypatch):
    schema = _request_schema(monkeypatch, _inputs(context_projected=b'{"native_event": {}}'))
    assert 'oneOf' not in schema


def test_native_source_request_keeps_both_supported_styles(monkeypatch):
    schema = _request_schema(monkeypatch, _inputs(projected=b'verified projection'))
    assert len(schema['oneOf']) == 2
    assert 'native_event' in schema['oneOf'][1]['required']


def test_request_selection_preserves_global_semantic_validation(monkeypatch):
    before = json.dumps(compiler.RAW_PLAN_SCHEMA, sort_keys=True)
    _request_schema(monkeypatch, _inputs())
    assert json.dumps(compiler.RAW_PLAN_SCHEMA, sort_keys=True) == before
