"""A language-server process that failed for good is replaced by the next query.

The session kept the dead process as its own and never started another, so a
key in steady use stayed degraded. See
docs/research/2026-09-25-a-failed-server-is-started-again.md.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

from lsp_process import ProcessState

from tests.code_kernel_helpers import repository, state_root  # noqa: F401 - fixtures
from tests.slow_machine import SHORT_TIMEOUT
from tests.test_pyright_session import _anchor, _session, semantic_pyright  # noqa: F401 - fixture


def test_a_failed_process_is_replaced_and_answers(repository: Path, state_root: Path, semantic_pyright) -> None:  # noqa: F811
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        failed = session._process
        failed.state = ProcessState.FAILED

        session.definition(_anchor(repository, "pkg/service.py", 10, 20), deadline=time.monotonic() + SHORT_TIMEOUT)
        waiting = session._process
        # The backoff has passed (audit 2026-09-26 C-2 paces the replacement).
        session._startup_retry_after = time.monotonic()
        answer = session.definition(_anchor(repository, "pkg/service.py", 10, 20), deadline=time.monotonic() + SHORT_TIMEOUT)

        assert (waiting, session._process is not failed, bool(answer.locations), answer.partial) == (
            None,
            True,
            True,
            False,
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_a_replacement_waits_for_the_first_backoff(repository: Path, state_root: Path, semantic_pyright) -> None:  # noqa: F811
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        session._process.state = ProcessState.FAILED
        before = time.monotonic()

        session.definition(_anchor(repository, "pkg/service.py", 10, 20), deadline=time.monotonic() + SHORT_TIMEOUT)

        assert (session._process, session._startup_retries, session._startup_retry_after >= before + 5.0) == (
            None,
            1,
            True,
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_a_server_that_ran_long_enough_gets_its_budget_back(
    repository: Path, state_root: Path, semantic_pyright  # noqa: F811
) -> None:
    import pyright_session

    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        session._startup_retries = len(pyright_session._STARTUP_RETRY_BACKOFF)
        session._process_started_at = time.monotonic() - pyright_session.HEALTHY_RUN_SECONDS - 1
        session._process.state = ProcessState.FAILED

        session.definition(_anchor(repository, "pkg/service.py", 10, 20), deadline=time.monotonic() + 10)

        assert (session._process, session._startup_retries) == (None, 1)
    finally:
        session.close(deadline=time.monotonic() + 5)


def _targets(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    return [node.target]


def _statements(function: ast.AST, kind: type) -> list[ast.AST]:
    return [node for node in ast.walk(function) if isinstance(node, kind)]


def _attribute_names(targets: list[ast.AST]) -> set[str]:
    return {target.attr for target in targets if isinstance(target, ast.Attribute)}


def _attributes(function: ast.AST, kind: type) -> set[str]:
    targets = [target for statement in _statements(function, kind) for target in _targets(statement)]
    return _attribute_names(targets)


def _spending_functions() -> list[ast.FunctionDef]:
    import pyright_session

    tree = ast.parse(Path(pyright_session.__file__).read_text(encoding="utf-8"))
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    return [node for node in functions if "_startup_retries" in _attributes(node, ast.AugAssign)]


def test_every_counted_retry_is_paced() -> None:
    """A path that spends a retry also names when it may run (audit 2026-09-26 C-2)."""
    spending = _spending_functions()
    unpaced = [node.name for node in spending if "_startup_retry_after" not in _attributes(node, ast.Assign)]

    assert (len(spending) >= 2, unpaced) == (True, [])
