"""A claim nobody was told about must actually be released.

`_publish_active_claim` promises that either the claim is held and recorded or
it is not held at all. The release runs under the same contention that just
stopped the announcement, so one attempt is the one most likely to fail too.
When it failed quietly, the caller's retry met its own rows as a conflict and
kept meeting them for the whole lease — which is what timing::windows_full
reported on py3.14.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import blackboard  # noqa: E402

from tests.test_reliability_v3_adoption import (  # noqa: E402
    _vault,
    build_adopted_reliability_v3,
)


@pytest.fixture()
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    projects = root / "knowledge/projects"
    (projects / "demo/.blackboard").mkdir(parents=True)
    monkeypatch.setattr(blackboard, "PROJECTS_DIR", projects)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    return root


def _fail_the_announcement(monkeypatch: pytest.MonkeyPatch):
    """Let everything through except the record that announces the claim."""
    real_append = blackboard._append_jsonl

    def failing_append(path, record, operation_id):
        if operation_id.startswith("blackboard-active:"):
            raise TimeoutError("Markdown writer gate deadline expired")
        return real_append(path, record, operation_id)

    monkeypatch.setattr(blackboard, "_append_jsonl", failing_append)
    return real_append


def _claim(resource: str = "shared/resource"):
    return blackboard.claim_task(
        "demo", "task", "agent-a", resources=[resource]
    )


def test_a_release_that_loses_the_first_attempts_still_happens(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release competes with whatever stopped the announcement."""
    real_delete = blackboard._delete_exact_claim
    attempts: list[int] = []

    def flaky_delete(coordinator, claim):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("database is locked")
        return real_delete(coordinator, claim)

    monkeypatch.setattr(blackboard, "_delete_exact_claim", flaky_delete)
    monkeypatch.setattr(blackboard, "_RELEASE_RETRY_SECONDS", 0.0)
    real_append = _fail_the_announcement(monkeypatch)

    with pytest.raises(TimeoutError):
        _claim()

    assert len(attempts) == 3

    # The release landed, so the resource is free and the caller's retry —
    # the whole point of releasing — now succeeds.
    monkeypatch.setattr(blackboard, "_append_jsonl", real_append)
    monkeypatch.setattr(blackboard, "_delete_exact_claim", real_delete)

    assert _claim().resources == ("shared/resource",)


def test_the_attempts_are_bounded(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release that never succeeds must not hold the caller forever."""
    attempts: list[int] = []

    def always_failing_delete(_coordinator, _claim):
        attempts.append(1)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(blackboard, "_delete_exact_claim", always_failing_delete)
    monkeypatch.setattr(blackboard, "_RELEASE_RETRY_SECONDS", 0.0)
    _fail_the_announcement(monkeypatch)

    with pytest.raises(TimeoutError):
        _claim()

    assert len(attempts) == blackboard._RELEASE_ATTEMPTS


def test_the_callers_own_failure_is_what_reaches_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release failure must not replace the announcement failure."""
    monkeypatch.setattr(
        blackboard,
        "_delete_exact_claim",
        lambda _coordinator, _claim: (_ for _ in ()).throw(
            RuntimeError("release exploded")
        ),
    )
    monkeypatch.setattr(blackboard, "_RELEASE_RETRY_SECONDS", 0.0)
    _fail_the_announcement(monkeypatch)

    with pytest.raises(TimeoutError, match="writer gate deadline expired"):
        _claim()
