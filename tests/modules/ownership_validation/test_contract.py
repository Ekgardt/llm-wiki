"""Captured original valid/invalid results protect the existing validator contract."""
import json
from pathlib import Path

import pytest
from llm_wiki import ownership_validation as owner
from llm_wiki.ownership_contracts import MarkerIdentity, ProcessIdentity
from llm_wiki.runtime_contracts import RuntimeFileIdentity

IDENTITY = RuntimeFileIdentity('posix', '1', '2', 8, 123)
PROCESS = ProcessIdentity(42, 'start')
MARKER = MarkerIdentity('run/marker.json', 'a' * 64, IDENTITY, 42)
CASES = [
    ('process', 'validate_process', (PROCESS,)),
    ('process-type', 'validate_process', ('process',)),
    ('pid-zero', 'validate_process', (ProcessIdentity(0, 'start'),)),
    ('pid-bool', 'validate_process', (ProcessIdentity(True, 'start'),)),
    ('start-empty', 'validate_process', (ProcessIdentity(1, ''),)),
    ('text', 'bounded_text', ('é', 'field', 2)),
    ('text-byte-bound', 'bounded_text', ('é', 'field', 1)),
    ('text-empty', 'bounded_text', ('', 'field', 2)),
    ('text-nul', 'bounded_text', ('a\0', 'field', 2)),
    ('text-surrogate', 'bounded_text', ('\ud800', 'field', 10)),
    ('role', 'validate_role', ('compile',)),
    ('role-unknown', 'validate_role', ('other',)),
    ('role-type', 'validate_role', (None,)),
    ('identity', 'file_identity_value', (IDENTITY,)),
    ('identity-type', 'file_identity_value', ('identity',)),
    ('identity-negative-size', 'file_identity_value', (RuntimeFileIdentity('p','v','i',-1,0),)),
    ('identity-bool-size', 'file_identity_value', (RuntimeFileIdentity('p','v','i',True,0),)),
    ('identity-large', 'file_identity_value', (RuntimeFileIdentity('p','v','i',4097,0),)),
    ('identity-negative-time', 'file_identity_value', (RuntimeFileIdentity('p','v','i',1,-1),)),
    ('marker-json', 'marker_json', (MARKER,)),
    ('marker-required', 'require_marker_presence', ('compile', None)),
    ('marker-forbidden', 'require_marker_presence', ('capture', MARKER)),
    ('marker-present', 'require_marker_presence', ('compile', MARKER)),
    ('marker-absent', 'require_marker_presence', ('capture', None)),
    ('marker-path', 'validated_marker_path', (MARKER, PROCESS)),
    ('marker-wrong-pid', 'validated_marker_path', (MARKER, ProcessIdentity(43,'start'))),
    ('marker-escape', 'validated_marker_path', (MarkerIdentity('../x','a'*64,IDENTITY,42), PROCESS)),
    ('marker-digest', 'validated_marker_path', (MarkerIdentity('run/x','A'*64,IDENTITY,42), PROCESS)),
    ('marker-bounded-json', 'validated_marker_json', (MARKER,)),
    ('marker-changed', 'require_marker_match', (None, IDENTITY, b'content', MARKER)),
]


def outcome(function, arguments):
    try:
        return {'value': repr(getattr(owner, function)(*arguments))}
    except (ValueError, TypeError, RuntimeError) as error:
        return {'error': type(error).__name__, 'message': str(error)}


@pytest.mark.parametrize('case,function,arguments', CASES, ids=[c[0] for c in CASES])
def test_original_validation_outcomes(case, function, arguments):
    fixture = json.loads(Path(__file__).with_name('original-contract.json').read_text(encoding='utf-8'))
    assert outcome(function, arguments) == fixture[case]
