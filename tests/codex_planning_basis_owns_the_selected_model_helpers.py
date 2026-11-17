"""Helpers shared by test_codex_planning_basis_owns_the_selected_model.py and the test files importing them."""

import llm_client as lc


def _descriptor():
    return lc.provider_candidates('codex')[0]


def _basis(tmp_path, descriptor=None, files=()):
    descriptor = descriptor or _descriptor()
    binary = tmp_path / 'selected-codex'
    binary.write_bytes(b'owned executable')
    return lc.CodexPlanningBasis(
        descriptor, lc.bind_codex_executable(str(binary)), 'resolved-model',
        'openai', 258400, 'a' * 64, files,
        lc.codex_basis_digest(lc.provider_environment()), 'codex-cli 0.160.0',
    )
