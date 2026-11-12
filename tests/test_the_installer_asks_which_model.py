"""The installer offers the models the user's provider answers with, and persists the choice.

Real provider CLIs are stood in for by shell programs on PATH, and HTTP providers by a
loopback server; the chooser runs its own code against them.
See docs/research/2026-09-29-the-installer-asks-which-model.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import choose_model  # noqa: E402
import integration_hook_config  # noqa: E402
import llm_client  # noqa: E402

from tests.shell_function_source import _shell_function  # noqa: E402
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


@pytest.mark.usefixtures("listing_server")
def test_ollama_offers_the_models_on_this_machine(monkeypatch) -> None:
    local = {"name": "qwen3:8b", "size": 5_000_000, "digest": "a" * 64}
    remote = {"name": "gpt-oss:120b-cloud", "remote_host": "https://ollama.com", "size": 1, "digest": "b" * 64}
    _Listing.payloads = {"/api/tags": {"models": [local, remote]}}
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "ollama")

    assert choose_model.offered_models(llm_client.provider_candidates("ollama")[0]) == ["qwen3:8b"]


@pytest.mark.usefixtures("listing_server")
def test_openai_offers_what_its_models_endpoint_lists(monkeypatch) -> None:
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
    monkeypatch.setattr(settings, 'default_root', lambda: root)
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


def _unattended_chooser(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / 'choose_model.py'), *arguments, '--root', str(root)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=LONG_TIMEOUT,
    )


def _vault_with_sonnet(tmp_path: Path) -> Path:
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'llm-wiki.toml').write_text('[llm]\nmodel="claude/sonnet"\n')
    return root


def test_a_saved_model_the_provider_now_refuses_does_not_stop_an_unattended_install(
    claude, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv('FAKE_ALLOWED', 'haiku')
    root = _vault_with_sonnet(tmp_path)

    done = _unattended_chooser(root)

    assert done.returncode == 0, done.stderr
    answer = json.loads(done.stdout)
    assert (answer['status'], answer['model']) == ('kept', 'sonnet')
    assert 'selection unchanged' in answer['reason']
    assert (root / 'llm-wiki.toml').read_text() == '[llm]\nmodel="claude/sonnet"\n'


def test_a_model_asked_for_by_name_and_refused_still_fails(claude, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv('FAKE_ALLOWED', 'haiku')
    root = _vault_with_sonnet(tmp_path)

    assert _unattended_chooser(root, '--model', 'opus').returncode == 1
    assert (root / 'llm-wiki.toml').read_text() == '[llm]\nmodel="claude/sonnet"\n'


@pytest.mark.parametrize(
    ('discovered', 'provider', 'said'),
    [
        (None, '', 'no provider is available'),
        (SimpleNamespace(provider='opencode'), 'opencode', 'cannot select an explicit model'),
    ],
    ids=['nothing signed in', 'adapter without model choice'],
)
def test_a_provider_with_no_choice_to_offer_does_not_stop_an_install(
    tmp_path, monkeypatch, capsys, discovered, provider, said
) -> None:
    monkeypatch.delenv('MEMORY_LLM_PROVIDER', raising=False)
    monkeypatch.setattr(choose_model, 'discovery_provider', lambda _requested: discovered)
    root = tmp_path / 'vault'
    root.mkdir()

    assert choose_model.main(['--root', str(root)]) == 0

    answer = json.loads(capsys.readouterr().out)
    assert (answer['status'], answer['model'], answer['provider']) == ('unconfigured', '', provider)
    assert said in answer['reason']


@pytest.mark.parametrize('installer', ['install.sh', 'install.ps1'])
def test_both_installers_tell_the_person_why_the_model_stayed(installer: str) -> None:
    text = (SCRIPTS.parent / installer).read_text(encoding='utf-8')

    assert 'The memory model was not changed:' in text
    assert 'reason' in text


_CHOOSER_DRIVER = """
import os, sys, types
sys.path.insert(0, os.environ['TEST_SCRIPTS'])
import choose_model
discovered = {'none': None, 'opencode': types.SimpleNamespace(provider='opencode')}
scenario = os.environ['TEST_DISCOVERY']
if scenario in discovered:
    choose_model.discovery_provider = lambda _requested: discovered[scenario]
raise SystemExit(choose_model.main(['--root', os.environ['TEST_VAULT'], *sys.argv[1:]]))
"""

_UV_STANDING_IN = """#!/usr/bin/env bash
while [ "$1" != python ]; do shift; done
shift 2
exec "$TEST_PYTHON" "$TEST_DRIVER" "$@"
"""

_SHELL_HELPERS = """set -euo pipefail
info() { echo "$1"; }; ok() { echo "$1"; }; warn() { echo "WARN $1"; }; fail() { echo "FAIL $1"; exit 1; }
"""


def _the_installers_model_step() -> str:
    text = (SCRIPTS.parent / 'install.sh').read_text(encoding='utf-8')
    return _shell_function(text, 'choose_memory_model') + '\nchoose_memory_model\n'


def _run_the_installers_model_step(tmp_path: Path, root: Path, discovery: str, *model_args: str):
    tools = tmp_path / 'installer-tools'
    tools.mkdir()
    (tools / 'driver.py').write_text(_CHOOSER_DRIVER, encoding='utf-8')
    (tools / 'uv').write_text(_UV_STANDING_IN, encoding='utf-8')
    (tools / 'uv').chmod(0o755)
    setup = f'VAULT_ROOT={root}\nBOOTSTRAP_PYTHON={sys.executable}\nMODEL_ARGS=({" ".join(model_args)})\n'
    env = {**os.environ, 'PATH': f'{tools}{os.pathsep}{os.environ["PATH"]}', 'TEST_SCRIPTS': str(SCRIPTS),
           'TEST_VAULT': str(root), 'TEST_DISCOVERY': discovery, 'TEST_PYTHON': sys.executable,
           'TEST_DRIVER': str(tools / 'driver.py')}
    return subprocess.run(['bash', '-c', _SHELL_HELPERS + setup + _the_installers_model_step()],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env, timeout=LONG_TIMEOUT)


@pytest.mark.skipif(os.name == 'nt', reason='runs the POSIX installer step; install.ps1 is held to the same text above')
@pytest.mark.parametrize(
    ('discovery', 'said'),
    [('opencode', 'cannot select an explicit model'),
     ('none', 'no provider is available'),
     ('real', 'selection unchanged')],
    ids=['opencode first', 'no provider signed in', 'saved model refused on a rerun'],
)
def test_the_installer_step_carries_on_and_says_why(claude, tmp_path, monkeypatch, discovery, said) -> None:
    monkeypatch.setenv('FAKE_ALLOWED', 'haiku')
    monkeypatch.delenv('MEMORY_LLM_PROVIDER', raising=False)
    root = _vault_with_sonnet(tmp_path)

    done = _run_the_installers_model_step(tmp_path, root, discovery)

    assert done.returncode == 0, done.stdout + done.stderr
    assert 'WARN The memory model was not changed:' in done.stdout and said in done.stdout
    assert (root / 'llm-wiki.toml').read_text() == '[llm]\nmodel="claude/sonnet"\n'


@pytest.mark.skipif(os.name == 'nt', reason='runs the POSIX installer step; install.ps1 is held to the same text above')
def test_the_installer_step_stops_on_a_model_asked_for_by_name_and_refused(claude, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv('FAKE_ALLOWED', 'haiku')
    monkeypatch.delenv('MEMORY_LLM_PROVIDER', raising=False)
    root = _vault_with_sonnet(tmp_path)

    done = _run_the_installers_model_step(tmp_path, root, 'real', '--model', 'opus')

    assert done.returncode == 1
    assert 'FAIL Model choice failed' in done.stdout
