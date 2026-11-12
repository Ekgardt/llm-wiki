"""Queue wire validation is independent of execution and preserves exact bounds."""
import hashlib

import pytest
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.formats.queue_payload import PayloadValidation, validate_payload_blob


def _validate(raw, *, parse=True):
    return validate_payload_blob(raw, hashlib.sha256(raw).hexdigest(), parse=parse)


def test_canonical_payload_and_raw_inspection_preserve_identity():
    raw = b'{"items":[1,true,null],"name":"ok"}'
    digest = '364a34cd522634b4ee44cf2d71407e8baf6bc379ad77c3095403f901299820f7'
    assert _validate(raw) == PayloadValidation(raw, digest, {'items': [1, True, None], 'name': 'ok'}, None)
    assert _validate(raw, parse=False) == PayloadValidation(raw, digest, None, None)
    with pytest.raises(AttributeError):
        _validate(raw).code = 'changed'


@pytest.mark.parametrize('raw', [
    b'{"a":1,"a":1}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}',
    b'{"a":1.5}', b'{"b":1,"a":2}', b'{ "a":1}', b'{}\n', b'[]', b'null',
    b'1', b'', b'{', b'{"a":"\xff"}', b'{"a":"\\ud800"}', b'\xef\xbb\xbf{}',
])
def test_noncanonical_payload_is_rejected_but_can_be_inspected(raw):
    rejected = _validate(raw)
    assert rejected.code == 'payload_hash_mismatch'
    assert rejected.raw == raw and rejected.payload is None
    assert _validate(raw, parse=False).code is None


@pytest.mark.parametrize('digest', ['0' * 64, 'A' * 64, 'abc', None, 1])
@pytest.mark.parametrize('parse', [False, True])
def test_wrong_or_invalid_digest_is_never_accepted(digest, parse):
    assert validate_payload_blob(b'{}', digest, parse=parse).code == 'payload_hash_mismatch'


@pytest.mark.parametrize('raw,parse', [(bytearray(b'{}'), True), ('{}', True), (b'{}', 1)])
def test_invalid_argument_types_fail_explicitly(raw, parse):
    with pytest.raises(TypeError):
        validate_payload_blob(raw, '0' * 64, parse=parse)


@pytest.mark.parametrize('extra', [0, 1])
def test_raw_byte_ceiling_is_exact(extra):
    result = _validate(b'x' * (1024 * 1024 + extra), parse=False)
    assert (result.code is None) == (extra == 0)


@pytest.mark.parametrize('extra', [0, 1])
@pytest.mark.parametrize('kind', ['value', 'key', 'array', 'object', 'depth'])
def test_existing_format_limits_accept_boundary_and_reject_overflow(kind, extra):
    value = _bounded_document(kind, extra)
    result = _validate(canonical_json_bytes(value))
    assert (result.code is None) == (extra == 0)


def _bounded_document(kind, extra):
    constructors = {
        'value': lambda: {'v': 'x' * (256 * 1024 + extra)},
        'key': lambda: {'x' * (256 * 1024 + extra): 0},
        'array': lambda: {'v': [None] * (1024 + extra)},
        'object': lambda: {str(i): None for i in range(1024 + extra)},
        'depth': lambda: _nested_document(30 + extra),
    }
    return constructors[kind]()


def _nested_document(depth):
    value = None
    for _ in range(depth):
        value = [value]
    return {'v': value}


def test_unicode_string_bound_counts_encoded_bytes():
    assert _validate(canonical_json_bytes({'v': '€' * (256 * 1024 // 3)})).code is None
    assert _validate(canonical_json_bytes({'v': '€' * (256 * 1024 // 3 + 1)})).code == 'payload_hash_mismatch'
