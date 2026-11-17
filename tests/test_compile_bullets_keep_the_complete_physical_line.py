"""Both sides of physical evidence use the same outer-whitespace policy."""
import pytest
from llm_wiki import compile_pipeline_claims, compile_pipeline_operations


@pytest.mark.parametrize("prefix", ["  - ", "\t+ ", "  * ", "  12. ", "\t7) ", "- ", ""])
def test_complete_bullet_line_has_one_shared_normalization(prefix):
    text = "完整 字符串 cafe\u0301 被保留."
    physical = (prefix + text + "  \n").encode()
    offset = physical.index(text.encode())
    quote, encoded, start = compile_pipeline_claims.completed_line(physical, offset, text.encode(), text)
    compile_pipeline_operations.require_whole_physical_line(physical, start, encoded)
    assert quote == text
    assert encoded == text.encode()


def test_blockquote_marker_is_preserved():
    physical = "  > 完整 字符串.  \n".encode()
    quote = "> 完整 字符串."
    compile_pipeline_operations.require_whole_physical_line(physical, physical.index(b">"), quote.encode())
    assert compile_pipeline_claims.without_bullet(physical.decode()) == quote


@pytest.mark.parametrize("quote", ["完整", "完整 字符串 cafe\u00e9 被保留."])
def test_incomplete_or_unicode_normalized_quote_is_still_refused(quote):
    physical = "  - 完整 字符串 cafe\u0301 被保留.\n".encode()
    with pytest.raises(ValueError, match="complete physical source line"):
        compile_pipeline_operations.require_whole_physical_line(physical, physical.index("完整".encode()), quote.encode())


def test_partial_long_physical_line_is_still_refused():
    physical = b"prefix " + b"x" * 17000 + b" suffix\n"
    with pytest.raises(ValueError, match="complete physical source line"):
        compile_pipeline_operations.require_whole_physical_line(physical, 0, physical[:16384])
