"""Windows text newline translation must not change counted provider input."""
from pathlib import Path

import llm_client
import pytest


@pytest.mark.parametrize('payload', ('one\ntwo\n', 'one\r\ntwo\n', '完整 e\u0301\nточный факт\n'))
def test_prepared_provider_input_retains_every_utf8_byte_on_windows(monkeypatch, payload):
    native_factory = llm_client.tempfile.NamedTemporaryFile

    def windows_text_file(*arguments, **keywords):
        keywords.setdefault('newline', '\r\n')
        return native_factory(*arguments, **keywords)

    monkeypatch.setattr(llm_client.tempfile, 'NamedTemporaryFile', windows_text_file)
    path = Path(llm_client._temp_text_file(payload))
    try:
        assert path.read_bytes() == payload.encode('utf-8')
    finally:
        path.unlink()
