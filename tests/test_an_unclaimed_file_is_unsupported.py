"""A file no managed server claims is unsupported, not an error (audit 2026-09-26 C-9).

docs/research/2026-09-26-an-unclaimed-file-is-unsupported.md
"""
from __future__ import annotations

import time

import pytest
from code_navigation import Capability, NavigationRequest, NavigationStatus
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import repository, state_root  # noqa: F401 - fixtures
from tests.slow_machine import SHORT_TIMEOUT
from tests.test_code_navigation import _navigation, semantic_pyright  # noqa: F401 - fixture


@pytest.mark.parametrize("capability", list(Capability))
def test_every_capability_on_an_unclaimed_file_is_unsupported(
    repository, state_root, semantic_pyright, capability  # noqa: F811
) -> None:
    (repository / "notes.txt").write_text("alpha beta\n", encoding="utf-8")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    direction = "incoming" if capability is Capability.CALLS else None
    request = NavigationRequest(
        resolve_repository_scope(repository), capability, "notes.txt", 1, 0, direction=direction
    )
    try:
        result = navigation.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)
        started = session._process is not None
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)

    assert (result.status, result.warnings, started) == (
        NavigationStatus.UNSUPPORTED,
        ("no managed language server claims this file type",),
        False,
    )
