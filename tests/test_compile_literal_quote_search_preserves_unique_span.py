"""Literal search preserves exact nonoverlap evidence and stops on ambiguity."""
import itertools
import re

import compile_memory as compiler
import pytest


class _AmbiguityWitness:
    def __init__(self, matches):
        self.matches = iter(matches)
        self.consumed = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.consumed += 1
        if self.consumed > 2:
            raise AssertionError('search consumed more evidence after ambiguity was proved')
        return next(self.matches)


def test_ambiguity_does_not_enumerate_the_remaining_matches(monkeypatch):
    original = re.finditer

    def observed(pattern, block):
        return _AmbiguityWitness(original(pattern, block))

    monkeypatch.setattr(compiler.re, 'finditer', observed)
    with pytest.raises(ValueError, match='immutable snapshot'):
        compiler._sole_quote_offset(b'a' * 1000, b'a')


def _oracle(block, quote):
    offsets = [match.start() for match in re.finditer(re.escape(quote), block)]
    return offsets[0] if len(offsets) == 1 else None


def _actual(block, quote):
    try:
        return compiler._sole_quote_offset(block, quote)
    except ValueError as error:
        assert str(error) == 'compile evidence does not match the immutable snapshot'
        return None


@pytest.mark.parametrize('block,quote', [
    (b'', b''), (b'a', b''), (b'', b'a'), (b'aaa', b'aa'),
    (b'aaaa', b'aa'), (b'ababa', b'aba'), (b'\x00a\x00', b'\x00'),
    (b'.*[x]?', b'.*[x]?'), ('До: факт'.encode(), 'факт'.encode()),
    (b'a\na', b'a'), (b'ab', b'abc'),
])
def test_exact_literal_nonoverlap_and_empty_cases(block, quote):
    assert _actual(block, quote) == _oracle(block, quote)


def test_all_small_binary_blocks_and_quotes_preserve_regex_semantics():
    words = [bytes(word) for length in range(5)
             for word in itertools.product((0, 97), repeat=length)]
    for block, quote in itertools.product(words, repeat=2):
        assert _actual(block, quote) == _oracle(block, quote)
