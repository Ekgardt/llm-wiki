"""Both resources are readable through a real stdio session with the real SDK.

The read handler returned the protocol model `TextResourceContents` where the
SDK 1.29 expects its helper `ReadResourceContents`, and every read failed with
`'TextResourceContents' object has no attribute 'content'`. The unit tests stood
a stand-in for the type and never met the SDK's reading of it. See
docs/research/2026-09-25-the-health-resource-is-readable.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
READ_TIMEOUT_SECONDS = 60


def _server(tmp_path: Path) -> subprocess.Popen:
    vault = tmp_path / "vault"
    (vault / "knowledge" / "notes").mkdir(parents=True)
    env = dict(
        os.environ,
        LLM_WIKI_ROOT=str(vault),
        LLM_WIKI_STATE_ROOT=str(tmp_path / "state"),
        LLMWIKI_NO_ENCODER_WARMUP="1",
        MEMORY_LLM_PROVIDER="fake",
    )
    return subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "mcp_server.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _send(process: subprocess.Popen, message: dict) -> None:
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def _reply(process: subprocess.Popen, request_id: int) -> dict:
    for line in process.stdout:
        message = json.loads(line)
        if message.get("id") == request_id:
            return message
    raise AssertionError("the server closed before it answered")


def _initialised(process: subprocess.Popen) -> None:
    _send(
        process,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "resource-probe", "version": "1"},
            },
        },
    )
    _reply(process, 0)
    _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})


@pytest.mark.parametrize("uri", ["llm-wiki://health", "llm-wiki://context"])
def test_a_resource_reads_as_json_text(tmp_path: Path, uri: str) -> None:
    process = _server(tmp_path)
    try:
        _initialised(process)
        _send(process, {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}})
        reply = _reply(process, 1)
    finally:
        process.kill()
        process.wait(READ_TIMEOUT_SECONDS)

    content = reply["result"]["contents"][0]
    assert (content["uri"], content["mimeType"], json.loads(content["text"])["schema_version"]) == (
        uri,
        "application/json",
        "1.0",
    )
