from __future__ import annotations

import argparse
import io

import codex_memory
import pytest

from tests.test_an_earlier_codex_entry_is_rewritten import EARLIER


@pytest.mark.parametrize("command,expected", (
    (codex_memory.command_config_state, b"stale\n"),
    (codex_memory.command_config_replace, b"replaced\n"),
))
def test_config_status_is_lf_even_when_native_stdout_uses_crlf(tmp_path, monkeypatch, command, expected):
    config = tmp_path / "config.toml"
    config.write_bytes(EARLIER.encode())
    args = argparse.Namespace(config=str(config), vault_root=str(tmp_path), foreign=False)
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding="utf-8", newline="\r\n")
    monkeypatch.setattr(codex_memory.sys, "stdout", stream)
    code = command(args)
    stream.flush()
    assert (code, output.getvalue()) == (0, expected)
    stream.detach()
