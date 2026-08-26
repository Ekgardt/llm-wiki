"""A claim nobody was told about must actually be released.

`_publish_active_claim` promises that either the claim is held and recorded or
it is not held at all. The recovery runs under the same contention that just
stopped the announcement, so the attempt most likely to fail is the one that
keeps the promise. When it failed quietly, the caller's retry met its own rows
as a conflict and kept meeting them for the whole lease — which is what
timing::windows_full reported on py3.14 (2026-08-22) and again on py3.11
(2026-08-26).

The recovery has two halves and both run under that contention. The second one
was found on 2026-08-26: before deciding, the handler read the task stream to
see whether the announcement had landed after all, and that read goes through
the same global writer gate. When the read failed, its exception replaced the
caller's own, the release ran zero times, and the rows stood for the whole
lease. Both halves are retried now, and an unreadable stream is no longer
mistaken for a landed record.
"""

import sys
import time
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


def _fail_the_presence_read(monkeypatch: pytest.MonkeyPatch):
    """Break the read that asks whether the announcement landed.

    It is a coherent read, so it takes the one global Markdown writer gate —
    the gate whose loss is the ordinary reason the announcement failed. Losing
    it here is not a contrived injection; it is the same contention arriving a
    few milliseconds later.
    """

    real_read = blackboard._read_jsonl

    def failing_read(_path):
        raise RuntimeError("gate ownership was lost")

    monkeypatch.setattr(blackboard, "_read_jsonl", failing_read)
    return real_read


def test_an_unreadable_stream_does_not_skip_the_release(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable task stream is not evidence that the record landed."""
    attempts: list[int] = []
    real_delete = blackboard._delete_exact_claim

    def counted_delete(coordinator, claim):
        attempts.append(1)
        return real_delete(coordinator, claim)

    monkeypatch.setattr(blackboard, "_delete_exact_claim", counted_delete)
    monkeypatch.setattr(blackboard, "_RELEASE_RETRY_SECONDS", 0.0)
    real_append = _fail_the_announcement(monkeypatch)
    real_read = _fail_the_presence_read(monkeypatch)

    with pytest.raises(TimeoutError):
        _claim()

    assert attempts, "the release never ran, so the rows stand for the lease"

    # The whole point of releasing: the caller's retry no longer meets itself.
    monkeypatch.setattr(blackboard, "_append_jsonl", real_append)
    monkeypatch.setattr(blackboard, "_read_jsonl", real_read)
    assert _claim().resources == ("shared/resource",)


def test_a_failing_presence_read_does_not_replace_the_callers_failure(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery's own trouble must not be reported as the cause."""
    monkeypatch.setattr(blackboard, "_RELEASE_RETRY_SECONDS", 0.0)
    _fail_the_announcement(monkeypatch)
    _fail_the_presence_read(monkeypatch)

    with pytest.raises(TimeoutError, match="writer gate deadline expired"):
        _claim()


def test_a_readable_stream_is_read_again_before_the_release(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream that becomes readable settles the claim without releasing it."""
    reads: list[int] = []
    real_read = blackboard._read_jsonl

    def slow_to_recover_read(path):
        reads.append(1)
        if len(reads) < 3:
            raise RuntimeError("gate ownership was lost")
        return real_read(path)

    def landed_append(path, record, operation_id):
        real_append(path, record, operation_id)
        if operation_id.startswith("blackboard-active:"):
            raise TimeoutError("Markdown writer gate deadline expired")

    real_append = blackboard._append_jsonl
    monkeypatch.setattr(blackboard, "_append_jsonl", landed_append)
    monkeypatch.setattr(blackboard, "_read_jsonl", slow_to_recover_read)
    monkeypatch.setattr(
        blackboard,
        "_delete_exact_claim",
        lambda _coordinator, _claim: pytest.fail("a landed claim was released"),
    )
    monkeypatch.setattr(blackboard, "_RELEASE_RETRY_SECONDS", 0.0)

    # The record is on disk, so the claim stands and the caller is told so.
    assert _claim().resources == ("shared/resource",)
    assert len(reads) == 3


def test_a_recovery_that_settles_nothing_says_so(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rows left behind must be named, because the next conflict is their child."""
    monkeypatch.setattr(
        blackboard,
        "_delete_exact_claim",
        lambda _coordinator, _claim: (_ for _ in ()).throw(
            RuntimeError("database is locked")
        ),
    )
    monkeypatch.setattr(blackboard, "_RELEASE_RETRY_SECONDS", 0.0)
    _fail_the_announcement(monkeypatch)
    _fail_the_presence_read(monkeypatch)

    with pytest.raises(TimeoutError):
        _claim()

    reported = capsys.readouterr().err
    assert "shared/resource" in reported
    assert f"{blackboard._RELEASE_ATTEMPTS} release attempts" in reported


def test_a_conflict_names_the_claim_that_holds_the_resource(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare "already claimed" cannot say whose rows these are."""
    held = _claim()

    with pytest.raises(blackboard.BlackboardConflictError) as raised:
        blackboard.claim_task(
            "demo", "task", "agent-b", resources=["shared/resource"]
        )

    message = str(raised.value)
    assert "shared/resource" in message
    assert "agent-a" in message
    assert held.claim_id[:16] in message
    assert raised.value.holders[0]["agent"] == "agent-a"


def test_a_slow_stream_is_asked_once_not_once_per_attempt(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrying the ask must not multiply one gate wait by the attempt count."""
    reads: list[int] = []

    def slow_failing_read(_path):
        reads.append(1)
        time.sleep(0.05)
        raise RuntimeError("gate ownership was lost")

    real_read = blackboard._read_jsonl
    monkeypatch.setattr(blackboard, "_read_jsonl", slow_failing_read)
    monkeypatch.setattr(blackboard, "_SETTLE_READ_SECONDS", 0.01)
    monkeypatch.setattr(blackboard, "_RELEASE_RETRY_SECONDS", 0.0)
    real_append = _fail_the_announcement(monkeypatch)

    with pytest.raises(TimeoutError):
        _claim()

    assert len(reads) == 1

    monkeypatch.setattr(blackboard, "_append_jsonl", real_append)
    monkeypatch.setattr(blackboard, "_read_jsonl", real_read)
    assert _claim().resources == ("shared/resource",)
