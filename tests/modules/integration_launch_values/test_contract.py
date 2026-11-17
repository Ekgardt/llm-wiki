"""Exact wire baseline and existing facade identities."""
import json
from pathlib import Path

import pytest
from llm_wiki import integration_launch_values as codec

BASELINE = json.loads(Path(__file__).with_name('baseline.json').read_text(encoding='utf-8'))


def test_emitted_program_and_provider_keys_are_unchanged():
    assert codec.codex_provider_bootstrap() == BASELINE['bootstrap']
    assert codec.CODEX_BOOTSTRAP_BODY == BASELINE['body']
    assert list(codec.PROVIDER_ENV_KEYS) == BASELINE['keys']


@pytest.mark.parametrize('case', BASELINE['payloads'])
def test_payload_bytes_and_decoding_match_original(case):
    assert codec.codex_provider_payload(case['bundle']) == case['payload']
    assert codec.codex_provider_from_payload(case['payload']) == case['bundle']


@pytest.mark.parametrize('case', BASELINE['windows'])
def test_windows_rendering_matches_original(case):
    assert codec.codex_windows_command(case['args']) == case['command']
