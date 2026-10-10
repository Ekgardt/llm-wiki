import re
import sys

import pytest
import secret_redact as redactor


class ObservedRule:
    def __init__(self, pattern):
        self.pattern = pattern.pattern
        self.flags = pattern.flags
        self.original = pattern
        self.calls = 0

    def sub(self, replacement, text):
        self.calls += 1
        return self.original.sub(replacement, text)


class RegexWork:
    def __init__(self, pattern):
        self.pattern = pattern
        self.calls = 0

    def observe(self, _frame, event, argument):
        if event == 'c_call' and getattr(argument, '__self__', None) is self.pattern and getattr(argument, '__name__', None) == 'sub':
            self.calls += 1


def test_absent_necessary_literal_avoids_unnecessary_regex_work(monkeypatch):
    pattern, replacement = redactor._PATTERNS[0]
    observed = RegexWork(pattern)
    monkeypatch.setattr(redactor, '_PATTERNS', [(pattern, replacement)])
    text = 'Cedar project completed the documented review.\n' * 200
    previous = sys.getprofile()
    sys.setprofile(observed.observe)
    try:
        actual = redactor._redact_patterns(text)
    finally:
        sys.setprofile(previous)
    assert actual == text
    assert observed.calls == 0


def test_custom_object_cannot_claim_certified_regex_metadata(monkeypatch):
    pattern, replacement = redactor._PATTERNS[0]
    observed = ObservedRule(pattern)
    monkeypatch.setattr(redactor, '_PATTERNS', [(observed, replacement)])
    assert redactor._redact_patterns('Cedar') == 'Cedar'
    assert observed.calls == 1


def test_unknown_rule_still_runs_the_complete_regex(monkeypatch):
    observed = ObservedRule(re.compile('Cedar'))
    monkeypatch.setattr(redactor, '_PATTERNS', [(observed, '[REDACTED]')])
    assert redactor._redact_patterns('Cedar') == '[REDACTED]'
    assert observed.calls == 1


def test_prerequisite_is_checked_after_each_replacement(monkeypatch):
    original, replacement = redactor._PATTERNS[0]
    token = 'sk-' + 'a' * 30
    monkeypatch.setattr(redactor, '_PATTERNS', [
        (re.compile('Cedar'), token), (original, replacement),
    ])
    assert redactor._redact_patterns('Cedar') == replacement


def test_changed_case_flags_use_original_regex(monkeypatch):
    original, replacement = redactor._PATTERNS[0]
    changed = re.compile(original.pattern, re.IGNORECASE)
    monkeypatch.setattr(redactor, '_PATTERNS', [(changed, replacement)])
    assert redactor._redact_patterns('SK-' + 'a' * 30) == replacement


def original_patterns(text):
    for pattern, replacement in redactor._PATTERNS:
        text = pattern.sub(replacement, text)
    return text


@pytest.mark.parametrize('text', [
    ' '.join(prefix + 'a' * 40 for prefix in (
        'sk-', 'ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_', 'github_pat_',
        'sk_live_', 'rk_test_', 'npm_', 'hf_', 'pypi-', 'GOCSPX-',
        'xapp-1-', 'xoxb-', 'ya29.', 'glpat-',
    )),
    'AKIA' + 'A' * 16 + ' AIza' + 'a' * 35,
    'eyJabc.eyJdef.ghi 123456:' + 'a' * 35,
    '-----BEGIN PRIVATE KEY-----\nCedar\r\n',
    'https://hooks.slack.com/services/AAA/BBB/CCC',
    '?API_KEY=cedar&ſig=birch HTTPS://user:password@host --password=cedar',
    'token=sk-' + 'a' * 40 + '\r\napi_Key="cedar"',
    'İ ı ſ K 😀\x00 Cedar project\r\n' * 30,
])
def test_entire_redaction_matches_original_ordered_pipeline(monkeypatch, text):
    actual = redactor.redact_secrets(text)
    monkeypatch.setattr(redactor, '_redact_patterns', original_patterns)
    assert actual == redactor.redact_secrets(text)


def full_packing(inputs):
    import compile_memory as compiler
    from llm_client import planning_input_text

    from tests.test_compile_reuses_raw_row_positions_within_one_layout import _packing_result

    result = _packing_result(inputs)
    wires = tuple(planning_input_text(prompt, compiler.DRAFT_SYSTEM, schema)
                  for prompt, schema in result[1])
    return result, wires


def rss_high_water():
    try:
        import resource
    except ImportError:
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def measure_packing(inputs):
    import time

    wall, cpu = time.monotonic(), time.process_time()
    result = full_packing(inputs)
    return result, time.monotonic() - wall, time.process_time() - cpu, rss_high_water()


def test_matched_complete_packing_layout_bindings_and_wire(tmp_path, monkeypatch, record_property):
    from dataclasses import replace

    import compile_memory as compiler

    from tests.test_compile_binds_the_protected_source_view import _legacy_packet
    from tests.test_compile_reuses_raw_row_positions_within_one_layout import _context_pages

    inputs = _legacy_packet(tmp_path, monkeypatch, [f'Факт {index} is settled.' for index in range(750)])
    parts = tuple(compiler._daily_parts(inputs.dailies[0].logical_path, inputs.dailies[0].content))
    inputs = replace(inputs, dailies=parts, sources=(*inputs.sources, *_context_pages()))
    current = redactor._redact_patterns
    results = []
    for name, function in [('original', original_patterns), ('candidate', current),
                           ('candidate', current), ('original', original_patterns)]:
        monkeypatch.setattr(redactor, '_redact_patterns', function)
        result, wall, cpu, rss = measure_packing(inputs)
        results.append(result)
        record_property(name + '_wall', wall)
        record_property(name + '_cpu', cpu)
        record_property(name + '_rss_high_water_kib', rss)
    assert all(result == results[0] for result in results)
    assert results[0][0][0]
