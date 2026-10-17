"""A flat JSON reader consumes scalar cells without copying every remaining cell."""
import io
import json

import pytest
import search_memory as search


class _TrackedText(str):
    def __new__(cls, value, observation):
        result = super().__new__(cls, value)
        result.observation = observation
        return result

    def __getitem__(self, key):
        value = super().__getitem__(key)
        if isinstance(key, slice):
            self.observation['sliced_characters'] += len(value)
        return type(self)(value, self.observation)

    def lstrip(self, characters=None):
        return type(self)(super().lstrip(characters), self.observation)


class _TinyReads:
    def __init__(self, text, width):
        self.source = io.StringIO(text)
        self.width = width

    def read(self, requested):
        return self.source.read(min(requested, self.width))


def test_consuming_one_loaded_array_has_no_remainder_copy_amplification():
    values = [f'{number:03d}-' + 'x' * 100 for number in range(128)]
    text = json.dumps(values)
    observed = {'sliced_characters': 0}
    reader = search._VectorJSONReader(io.StringIO(''), None)
    reader.buffer = _TrackedText(text, observed)
    reader.ended = True
    reader.take('[')
    assert list(search._read_vector_array_members(reader)) == values
    reader.take(']')
    assert reader.peek() == ''
    assert observed['sliced_characters'] <= len(text)


@pytest.mark.parametrize('value', [1234567, 'кириллица😀\\quote"', ''])
@pytest.mark.parametrize('width', [1, 2, 7])
def test_scalar_boundaries_keep_numbers_and_escaped_unicode_whole(value, width):
    reader = search._VectorJSONReader(_TinyReads(json.dumps([value]), width), None)
    reader.take('[')
    assert reader.scalar() == value
    reader.take(']')
    assert reader.peek() == ''


def test_one_scalar_across_the_transport_block_is_not_truncated():
    value = 'ё' * (64 * 1024 + 1)
    reader = search._VectorJSONReader(io.StringIO(json.dumps([value], ensure_ascii=False)), None)
    reader.take('[')
    assert reader.scalar() == value
    reader.take(']')
    assert reader.peek() == ''
