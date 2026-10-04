"""Both sides of physical evidence use the same outer-whitespace policy."""
import compile_memory as compiler
import pytest


@pytest.mark.parametrize("prefix", ["  - ", "\t+ ", "  * ", "  12. ", "\t7) ", "- ", ""])
def test_complete_bullet_line_has_one_shared_normalization(prefix):
    text = "Полная строка cafe\u0301 сохраняется."
    physical = (prefix + text + "  \n").encode()
    offset = physical.index(text.encode())
    quote, encoded, start = compiler._completed_line(physical, offset, text.encode(), text)
    compiler._require_whole_physical_line(physical, start, encoded)
    assert quote == text
    assert encoded == text.encode()


def test_blockquote_marker_is_preserved():
    physical = "  > Полная строка.  \n".encode()
    quote = "> Полная строка."
    compiler._require_whole_physical_line(physical, physical.index(b">"), quote.encode())
    assert compiler._without_bullet(physical.decode()) == quote


@pytest.mark.parametrize("quote", ["Полная", "Полная строка cafe\u00e9 сохраняется."])
def test_incomplete_or_unicode_normalized_quote_is_still_refused(quote):
    physical = "  - Полная строка cafe\u0301 сохраняется.\n".encode()
    with pytest.raises(ValueError, match="complete physical source line"):
        compiler._require_whole_physical_line(physical, physical.index("Полная".encode()), quote.encode())


def test_partial_long_physical_line_is_still_refused():
    physical = b"prefix " + b"x" * 17000 + b" suffix\n"
    with pytest.raises(ValueError, match="complete physical source line"):
        compiler._require_whole_physical_line(physical, 0, physical[:16384])
