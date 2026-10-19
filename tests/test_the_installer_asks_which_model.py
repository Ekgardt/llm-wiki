"""The installer offers the models the user's provider answers with, and persists the choice.

Real provider CLIs are stood in for by shell programs on PATH, and HTTP providers by a
loopback server; the chooser runs its own code against them.
See docs/research/2026-09-29-the-installer-asks-which-model.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import choose_model  # noqa: E402
import integration_hook_config  # noqa: E402
import llm_client  # noqa: E402

from tests.slow_machine import LONG_TIMEOUT, SHORT_TIMEOUT  # noqa: E402

pytestmark = [
    pytest.mark.usefixtures("unconfigured_model_settings"),
    pytest.mark.skipif(sys.platform == "win32", reason="a shell stub stands in for the CLI"),
]

# A Claude CLI that knows its flags and answers only the models in $FAKE_ALLOWED.
_CLAUDE = """#!/bin/sh
if [ "$1" = "--help" ]; then
  echo "--model --system-prompt --append-system-prompt --setting-sources"
  exit 0
fi
model=""
while [ $# -gt 0 ]; do
  [ "$1" = "--model" ] && model="$2"
  shift
done
cat >/dev/null
case " $FAKE_ALLOWED " in
  *" $model "*) echo OK ;;
  *) echo "unknown model $model" >&2; exit 1 ;;
esac
"""
_CODEX = """#!/bin/sh
echo '{"models": [{"slug": "gpt-6-sol"}, {"slug": "gpt-6-mini"}, {"note": "no name"}]}'
"""


def _on_path(tmp_path: Path, monkeypatch, name: str, body: str) -> None:
    binary = tmp_path / "bin" / name
    binary.parent.mkdir(exist_ok=True)
    binary.write_text(body, encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}:/usr/bin:/bin")


@pytest.fixture
def claude(tmp_path: Path, monkeypatch) -> llm_client.ProviderDescriptor:
    _on_path(tmp_path, monkeypatch, "claude", _CLAUDE)
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "claude")
    monkeypatch.setenv("FAKE_ALLOWED", "sonnet haiku")
    monkeypatch.delenv("MEMORY_CLAUDE_MODEL", raising=False)
    return choose_model.detected_provider()


def test_claude_offers_only_the_aliases_that_answered(claude) -> None:
    assert (claude.provider, choose_model.offered_models(claude)) == ("claude", ["sonnet", "haiku"])


def test_codex_offers_its_catalog(tmp_path: Path, monkeypatch) -> None:
    _on_path(tmp_path, monkeypatch, "codex", _CODEX)
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "codex")

    assert choose_model.offered_models(choose_model.detected_provider()) == ["gpt-6-sol", "gpt-6-mini"]


class _Listing(BaseHTTPRequestHandler):
    payloads: dict[str, object] = {}

    def do_GET(self) -> None:  # noqa: N802 - the http.server name
        body = json.dumps(self.payloads.get(self.path, {})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return None


@pytest.fixture
def listing_server(monkeypatch) -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Listing)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", endpoint)
    yield endpoint
    server.shutdown()
    thread.join(SHORT_TIMEOUT)


def test_ollama_offers_the_models_on_this_machine(listing_server, monkeypatch) -> None:
    local = {"name": "qwen3:8b", "size": 5_000_000, "digest": "a" * 64}
    remote = {"name": "gpt-oss:120b-cloud", "remote_host": "https://ollama.com", "size": 1, "digest": "b" * 64}
    _Listing.payloads = {"/api/tags": {"models": [local, remote]}}
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "ollama")

    assert choose_model.offered_models(llm_client.provider_candidates("ollama")[0]) == ["qwen3:8b"]


def test_openai_offers_what_its_models_endpoint_lists(listing_server, monkeypatch) -> None:
    _Listing.payloads = {"/v1/models": {"data": [{"id": "gpt-6-mini"}, {"id": "gpt-6"}]}}
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    assert choose_model.offered_models(llm_client.provider_candidates("openai")[0]) == ["gpt-6", "gpt-6-mini"]


def test_selection_is_saved_once_in_the_vault(claude, tmp_path, monkeypatch):
    import settings

    root = tmp_path / 'vault'
    root.mkdir()
    printed = subprocess.run(
        [sys.executable, str(SCRIPTS / 'choose_model.py'), '--model', 'sonnet',
         '--provider', 'claude', '--root', str(root)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
        check=True, timeout=LONG_TIMEOUT,
    )
    assert json.loads(printed.stdout)['model'] == 'claude/sonnet'
    monkeypatch.setattr(settings, '_default_root', lambda: root)
    assert llm_client.provider_candidates('claude')[0].model == 'sonnet'
    assert 'MEMORY_CLAUDE_MODEL' not in integration_hook_config.provider_environment()


def test_refused_selection_leaves_the_settings_unchanged(claude, tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    settings = root / 'llm-wiki.toml'
    original = '[llm]\nmodel="claude/sonnet"\n'
    settings.write_text(original)
    printed = subprocess.run(
        [sys.executable, str(SCRIPTS / 'choose_model.py'), 'set', 'claude/opus', '--root', str(root)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=LONG_TIMEOUT,
    )
    assert printed.returncode == 1
    assert settings.read_text() == original
    assert 'selection unchanged' in printed.stderr
