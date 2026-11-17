from __future__ import annotations

import time
from pathlib import Path
from typing import cast

import pytest
from llm_wiki.pyright_session_core import LanguageServerSession
from llm_wiki.pyright_session_documents import SyncPlan
from llm_wiki.pyright_session_values import OpenDocument
from lsp_process import LspProcess
from workspace_revision import WorkspaceRevision

from tests.language_server_session_state import (
    READY_GENERATION,
    ScriptedProcess,
    document_of,
    unstarted_session,
)
from tests.slow_machine import SHORT_TIMEOUT


@pytest.fixture
def session(repository: Path, state_root: Path) -> LanguageServerSession:
    return unstarted_session(repository, state_root)


def _process() -> LspProcess:
    return cast(LspProcess, ScriptedProcess())


def _revision(session: LanguageServerSession) -> WorkspaceRevision:
    scope = session._repository
    return WorkspaceRevision(scope.repository_id, scope.checkout_id, None, (), "0" * 64)


def _plan(session: LanguageServerSession, process: LspProcess) -> SyncPlan:
    return SyncPlan(
        process=process,
        generation=READY_GENERATION,
        prior=None,
        revision=_revision(session),
        deadline=time.monotonic() + SHORT_TIMEOUT,
        documents_snapshot={},
        next_documents={},
        projected_document_bytes=0,
        closed_documents=[],
        changed_replacements=[],
        close_notifications=[],
        watched_params=None,
    )


def test_recovering_a_process_the_session_no_longer_holds_is_refused(
    session: LanguageServerSession,
) -> None:
    session._process = _process()

    with pytest.raises(RuntimeError, match="process changed before recovery"):
        session._check_recovery_owners_locked(_process())


def test_recovering_while_another_cleanup_owner_is_held_is_refused(
    session: LanguageServerSession,
) -> None:
    process = _process()
    session._process = process
    session._startup_process = _process()

    with pytest.raises(RuntimeError, match="cleanup owner is unavailable"):
        session._check_recovery_owners_locked(process)


def test_a_restart_the_session_does_not_own_proves_nothing(
    session: LanguageServerSession,
) -> None:
    assert session._replayed_after_restart(_process()) is False
    assert session._process is None


def test_a_document_opened_while_planning_counts_as_a_change(
    session: LanguageServerSession,
) -> None:
    document: OpenDocument = document_of(session, "pkg/service.py")

    assert session._documents_unchanged_locked({document.source.uri: document}) is False


def test_a_closing_session_counts_as_a_changed_commit_state(
    session: LanguageServerSession,
) -> None:
    session._closing = True

    assert session._commit_state_changed_locked(_plan(session, _process())) is True


def test_a_commit_on_a_retired_wire_generation_is_refused(
    session: LanguageServerSession,
) -> None:
    session._begin_wire_generation("generation-replaced")

    error = session._commit_wire_generation_locked(_plan(session, _process()))

    assert isinstance(error, RuntimeError)
    assert "generation changed before commit" in str(error)


def test_a_commit_failure_is_raised_after_the_snapshot_is_restored(
    session: LanguageServerSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    restored: list[str] = []

    def restore(process: LspProcess, failed_generation: str, *, deadline: float) -> None:
        restored.append(failed_generation)

    monkeypatch.setattr(session, "_recover_synchronize_snapshot", restore)

    with pytest.raises(RuntimeError, match="commit refused"):
        session._recover_after_commit_failure(
            _plan(session, _process()), RuntimeError("commit refused")
        )

    assert restored == [READY_GENERATION]


def test_a_commit_failure_whose_recovery_failed_names_the_recovery(
    session: LanguageServerSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(process: LspProcess, failed_generation: str, *, deadline: float) -> None:
        raise OSError("restart refused")

    monkeypatch.setattr(session, "_recover_synchronize_snapshot", refuse)

    with pytest.raises(RuntimeError, match="commit recovery failed") as raised:
        session._recover_after_commit_failure(
            _plan(session, _process()), RuntimeError("commit refused")
        )

    assert isinstance(raised.value.__cause__, OSError)


def test_a_synchronize_past_its_deadline_is_refused(session: LanguageServerSession) -> None:
    with pytest.raises(TimeoutError, match="synchronize deadline expired"):
        session._check_synchronize_revision(_revision(session), time.monotonic() - 1.0)


def test_a_synchronize_for_something_that_is_not_a_revision_is_refused(
    session: LanguageServerSession,
) -> None:
    with pytest.raises(TypeError, match="revision must be a WorkspaceRevision"):
        session._require_matching_revision(cast(WorkspaceRevision, "revision"))
