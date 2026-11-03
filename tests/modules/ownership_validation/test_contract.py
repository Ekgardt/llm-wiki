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
    ('process', '_validate_process', (PROCESS,)),
    ('process-type', '_validate_process', ('process',)),
    ('pid-zero', '_validate_process', (ProcessIdentity(0, 'start'),)),
    ('pid-bool', '_validate_process', (ProcessIdentity(True, 'start'),)),
    ('start-empty', '_validate_process', (ProcessIdentity(1, ''),)),
    ('text', '_bounded_text', ('é', 'field', 2)),
    ('text-byte-bound', '_bounded_text', ('é', 'field', 1)),
    ('text-empty', '_bounded_text', ('', 'field', 2)),
    ('text-nul', '_bounded_text', ('a\0', 'field', 2)),
    ('text-surrogate', '_bounded_text', ('\ud800', 'field', 10)),
    ('role', '_validate_role', ('compile',)),
    ('role-unknown', '_validate_role', ('other',)),
    ('role-type', '_validate_role', (None,)),
    ('identity', '_file_identity_value', (IDENTITY,)),
    ('identity-type', '_file_identity_value', ('identity',)),
    ('identity-negative-size', '_file_identity_value', (RuntimeFileIdentity('p','v','i',-1,0),)),
    ('identity-bool-size', '_file_identity_value', (RuntimeFileIdentity('p','v','i',True,0),)),
    ('identity-large', '_file_identity_value', (RuntimeFileIdentity('p','v','i',4097,0),)),
    ('identity-negative-time', '_file_identity_value', (RuntimeFileIdentity('p','v','i',1,-1),)),
    ('marker-json', '_marker_json', (MARKER,)),
    ('marker-required', '_require_marker_presence', ('compile', None)),
    ('marker-forbidden', '_require_marker_presence', ('capture', MARKER)),
    ('marker-present', '_require_marker_presence', ('compile', MARKER)),
    ('marker-absent', '_require_marker_presence', ('capture', None)),
    ('marker-path', '_validated_marker_path', (MARKER, PROCESS)),
    ('marker-wrong-pid', '_validated_marker_path', (MARKER, ProcessIdentity(43,'start'))),
    ('marker-escape', '_validated_marker_path', (MarkerIdentity('../x','a'*64,IDENTITY,42), PROCESS)),
    ('marker-digest', '_validated_marker_path', (MarkerIdentity('run/x','A'*64,IDENTITY,42), PROCESS)),
    ('marker-bounded-json', '_validated_marker_json', (MARKER,)),
    ('marker-changed', '_require_marker_match', (None, IDENTITY, b'content', MARKER)),
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
