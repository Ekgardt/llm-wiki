"""Helpers shared by test_compile_shared_run_planning.py and the test files importing them."""

from dataclasses import replace

import llm_client as client


def _candidate():
    return replace(client.provider_candidates('fake', max_tokens=4000)[0], model='selected-model')


def _basis(candidate):
    executable = client.CodexExecutable('/private/bin/codex', '/private/tool/codex', (1, 2), 'a' * 64)
    return client.CodexPlanningBasis(candidate, executable, candidate.model, 'provider', 65536,
        'b' * 64, (('/private/config', 'c' * 64),), 'd' * 64, 'fixture-version')
