"""Four bounds that dropped results without saying so now say it, or keep the result.

- Pyright diagnostics past the budget: the query answers at once, marked `partial`,
  instead of waiting out its deadline on a publication that was never stored.
- A download error's cause chain deeper than it prints ends with a line saying so.
- Fresh positions past the per-answer file budget mark the rows they did not re-read.
- The navigation source cache evicts its oldest source instead of refusing the next.

See `docs/research/2026-09-27-a-cut-says-what-it-left-out.md`.
"""

from __future__ import annotations

import time
from pathlib import Path

import fresh_positions
import install_pyright
import mcp_server
import pytest
from llm_wiki import pyright_session_diagnostics
from llm_wiki.pyright_session_core import PyrightSession
from llm_wiki.pyright_session_values import ProviderDiagnostics
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import create_semantic_pyright_fixture
from tests.slow_machine import SHORT_TIMEOUT

_RANGE = {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}}


def _publish(session: PyrightSession, uri: str, message: str) -> None:
    session._publish_diagnostics({"uri": uri, "version": 1, "diagnostics": [{**_RANGE, "message": message}]})


def test_a_diagnostics_publication_past_the_budget_is_answered_as_partial_at_once(
    monkeypatch: pytest.MonkeyPatch, repository: Path, state_root: Path
) -> None:
    fixture = create_semantic_pyright_fixture(repository, config={"push_diagnostics": False})
    session = PyrightSession(resolve_repository_scope(repository), fixture.identity, state_root=state_root)
    monkeypatch.setattr(pyright_session_diagnostics, "MAX_DIAGNOSTIC_BYTES", 512)
    try:
        document = session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        _publish(session, document.source.uri, "x" * 2048)
        started = time.monotonic()
        answer = session.diagnostics("pkg/service.py", deadline=started + SHORT_TIMEOUT)
        assert answer == ProviderDiagnostics((), document.version, True)
        assert time.monotonic() - started < SHORT_TIMEOUT / 2
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def _chained(depth: int) -> BaseException:
    error: BaseException = OSError("root cause")
    for index in range(depth):
        wrapper = OSError(f"layer {index}")
        wrapper.__cause__ = error
        error = wrapper
    return error


def test_a_cause_chain_deeper_than_it_prints_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    install_pyright.print_cause_chain(_chained(install_pyright.MAX_CAUSE_DEPTH + 2))
    printed = capsys.readouterr().err
    assert printed.count("caused by:") == install_pyright.MAX_CAUSE_DEPTH
    assert "further causes not shown" in printed


def test_a_short_cause_chain_prints_no_note(capsys: pytest.CaptureFixture[str]) -> None:
    install_pyright.print_cause_chain(_chained(2))
    assert "further causes not shown" not in capsys.readouterr().err


def test_rows_past_the_file_budget_say_their_line_was_not_re_read(tmp_path: Path) -> None:
    count = fresh_positions.MAX_FILES + 1
    for index in range(count):
        (tmp_path / f"m{index}.py").write_text("def f():\n    pass\n", encoding="utf-8")
    rows = [{"file": f"m{index}.py", "name": "f", "line": 1} for index in range(count)]
    refreshed = fresh_positions.refreshed_rows(rows, tmp_path)
    assert [row.get("line_not_refreshed", False) for row in refreshed] == [False] * (count - 1) + [True]


def test_the_navigation_cache_evicts_the_oldest_source_instead_of_refusing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = resolve_repository_scope(tmp_path)
    monkeypatch.setattr("llm_wiki.mcp_server_limits.MAX_NAVIGATION_SOURCE_CACHE_BYTES", 8)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.navigation_source_bytes", lambda *_a, **_k: b"12345")
    cache = mcp_server.NavigationSourceCache()
    deadline = time.monotonic() + SHORT_TIMEOUT
    answers = [cache.read(scope, name, deadline=deadline) for name in ("a.py", "b.py", "c.py")]
    assert [answer[0] for answer in answers] == [b"12345"] * 3
    assert [key[2] for key in cache._values] == ["c.py"]
