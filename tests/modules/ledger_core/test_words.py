import pytest


@pytest.mark.parametrize("text, expected", [("my road-bikes", "road-bike"),
                                           ("O'Brian's", "o'brian'"),
                                           ("bike2_name", "bike name"),
                                           ("cafe\u0301", "café")])
def test_ledger_unicode_words_preserve_existing_ascii_boundaries(text, expected):
    from llm_wiki import ledger_core as ledger

    assert ledger.canonical(text) == expected
