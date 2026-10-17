"""The displayed verified projection supplies every required citation address."""
import json

import compile_memory as compiler
import pytest

from tests.test_native_compile_uses_whole_container import _native_inputs


def _projected_inputs(root, monkeypatch, prompt):
    inputs, _ = _native_inputs(root, monkeypatch, prompt)
    return compiler._subset_compile_inputs(inputs, {part.part_key for part in inputs.dailies})


def _displayed_event(inputs):
    rows = [json.loads(line.lstrip()) for line in compiler._input_blob(inputs).split('\n')
            if line.lstrip().startswith('{"')]
    assert len(rows) == 1
    return rows[0]


def _displayed_evidence(inputs):
    displayed = _displayed_event(inputs)
    address = displayed['evidence_address']
    index = address['user_line_indices'][-1]
    line = displayed['user_lines'][index].splitlines()[0]
    return {'daily_date': address['daily_date'], 'timestamp': address['timestamp'],
            'quoted_text': line, 'claim': 'The test bicycle is blue.',
            'native_event': {**displayed['native_event'], 'line_index': index}}


@pytest.mark.parametrize('prompt', ['First.\nThe test bicycle is blue.',
                                   'First.\r\nThe test bicycle is blue.\r\n',
                                   'First.\u2028The test bicycle is blue.'])
def test_displayed_native_address_binds_the_complete_verified_source(tmp_path, monkeypatch, prompt):
    inputs = _projected_inputs(tmp_path, monkeypatch, prompt)
    displayed = _displayed_event(inputs)
    evidence = _displayed_evidence(inputs)
    binding = compiler._evidence_binding(evidence, inputs)
    container = compiler._verified_claim_quote(binding, inputs)
    assert json.loads(container)['payload']['prompt'] == prompt
    assert displayed['evidence_address'] == {
        'daily_date': '2026-09-29', 'timestamp': inputs.dailies[0].native_frames[0].timestamp,
        'user_line_indices': list(range(len(prompt.splitlines())))}
    assert displayed['user_lines'] == prompt.splitlines(keepends=True)
    assert json.dumps(displayed, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')) in compiler._draft_prompt(inputs)


@pytest.mark.parametrize('change', ['timestamp', 'line_index', 'quote'])
def test_a_displayed_address_does_not_authorize_a_forged_citation(tmp_path, monkeypatch, change):
    inputs = _projected_inputs(tmp_path, monkeypatch, 'First.\nThe test bicycle is blue.')
    evidence = _displayed_evidence(inputs)
    mutations = {'timestamp': {'timestamp': '00:00:01'},
                 'line_index': {'native_event': {**evidence['native_event'], 'line_index': 2}},
                 'quote': {'quoted_text': 'bicycle is blue.'}}
    evidence.update(mutations[change])
    with pytest.raises(ValueError):
        compiler._evidence_binding(evidence, inputs)
