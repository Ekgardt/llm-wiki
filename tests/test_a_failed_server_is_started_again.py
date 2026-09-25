"""A language-server process that failed for good is replaced by the next query.

The session kept the dead process as its own and never started another, so a
key in steady use stayed degraded. See
docs/research/2026-09-25-a-failed-server-is-started-again.md.
"""

from __future__ import annotations

import time
from pathlib import Path

from lsp_process import ProcessState

from tests.code_kernel_helpers import repository, state_root  # noqa: F401 - fixtures
from tests.test_pyright_session import _anchor, _session, semantic_pyright  # noqa: F401 - fixture


def test_a_failed_process_is_replaced_and_answers(repository: Path, state_root: Path, semantic_pyright) -> None:  # noqa: F811
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        failed = session._process
        failed.state = ProcessState.FAILED

        answer = session.definition(_anchor(repository, "pkg/service.py", 10, 20), deadline=time.monotonic() + 10)

        assert (session._process is not failed, bool(answer.locations), answer.partial) == (True, True, False)
    finally:
        session.close(deadline=time.monotonic() + 5)
