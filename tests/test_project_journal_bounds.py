"""A project journal stays inside its byte and event bounds, rotating into sealed segments."""
from __future__ import annotations

from pathlib import Path

import project_journal
import pytest
from project_journal import ProjectJournalReadError, ProjectStore

from tests.project_journal_helpers import (
    checkpoint_event,
    state_body,
    state_root,  # noqa: F401
    vault,  # noqa: F401
)
from tests.project_journal_helpers import project_store as project_store


def test_projection_read_rejects_oversize_before_hash(
    vault: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    projection = vault / "knowledge/projects/demo/state.md"
    projection.write_bytes(b"x" * (project_journal.MAX_PROJECTION_BYTES + 1))
    hashed: list[bytes] = []
    sha256_bytes = project_journal.sha256_bytes

    def track_hash(content: bytes) -> str:
        hashed.append(content)
        return sha256_bytes(content)

    monkeypatch.setattr(project_journal, "sha256_bytes", track_hash)

    with pytest.raises(ProjectJournalReadError) as oversized:
        ProjectStore(vault, state_root).checkpoint(
            "demo", checkpoint_event(), "agent-a"
        )

    assert oversized.value.code == "too_large"
    assert projection.read_bytes() not in hashed


def test_a_full_journal_is_sealed_and_the_next_event_lands(
    vault: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    """A full journal used to be the end of the project's record.

    Appending past the bound was refused and nothing rotated, so the refusal
    was permanent. Measured on the live vault 2026-08-29:
    `knowledge/projects/llm-wiki/journal.md` held exactly the bound, 4 698
    checkpoint events waited behind that refusal, `run/state.json` reached
    10 MB, and the failure repeated every ten seconds into a log no health
    check read.
    """
    monkeypatch.setattr(project_journal, "MAX_JOURNAL_EVENTS", 2)
    store = ProjectStore(vault, state_root)

    first = store.checkpoint("demo", checkpoint_event(), "agent-a")
    second = store.checkpoint(
        "demo", checkpoint_event("evt-2", "limit:event-2"), "agent-a"
    )
    third = store.checkpoint(
        "demo", checkpoint_event("evt-3", "limit:event-3"), "agent-a"
    )

    journal = vault / "knowledge/projects/demo/journal.md"
    segment = vault / "knowledge/projects/demo/journal.000001-000002.md"
    assert [first.sequence, second.sequence, third.sequence] == [1, 2, 3]
    assert segment.is_file()
    _assert_sealed_and_live(segment, journal)


def _sequences(path: Path) -> list[int]:
    events = project_journal.parse_journal_events("demo", path.read_bytes())
    return [int(item["sequence"]) for item in events]


def _assert_sealed_and_live(segment: Path, journal: Path) -> None:
    """The sealed range, and a live journal that opens with its restated fold."""
    live = project_journal.parse_journal_events("demo", journal.read_bytes())
    assert _sequences(segment) == [1, 2]
    assert _sequences(journal) == [2, 3]
    assert live[0]["trigger"] == "journal_rotation"


def test_rotation_does_not_change_the_projection(
    vault: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    """The whole point: the fold of the live segment must equal the fold of all."""
    monkeypatch.setattr(project_journal, "MAX_JOURNAL_EVENTS", 1000)
    store = ProjectStore(vault, state_root)
    for index in range(1, 4):
        store.checkpoint(
            "demo", checkpoint_event(f"evt-{index}", f"rot:event-{index}"), "agent-a"
        )
    journal = vault / "knowledge/projects/demo/journal.md"
    whole = project_journal.parse_journal_events("demo", journal.read_bytes())
    expected = store.render_state(whole, _validated=True)

    monkeypatch.setattr(project_journal, "MAX_JOURNAL_EVENTS", 3)
    store.checkpoint("demo", checkpoint_event("evt-4", "rot:event-4"), "agent-a")

    live = project_journal.parse_journal_events("demo", journal.read_bytes())
    rolled = store.render_state(live[:1], _validated=True)
    assert state_body(rolled) == state_body(expected)


def test_a_journal_rolls_by_size_before_it_rolls_by_count(
    vault: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    """Two events in, the third finds the journal past the byte bound and seals it."""
    monkeypatch.setattr(project_journal, "MAX_JOURNAL_EVENTS", 1000)
    store = ProjectStore(vault, state_root)
    for index in range(1, 3):
        store.checkpoint("demo", checkpoint_event(f"evt-{index}", f"size:event-{index}"), "agent-a")
    journal = vault / "knowledge/projects/demo/journal.md"
    whole = project_journal.parse_journal_events("demo", journal.read_bytes())
    expected = store.render_state(whole, _validated=True)

    monkeypatch.setattr(project_journal, "ROTATE_ABOVE_BYTES", 1)
    store.checkpoint("demo", checkpoint_event("evt-3", "size:event-3"), "agent-a")

    live = project_journal.parse_journal_events("demo", journal.read_bytes())
    assert live[0]["trigger"] == "journal_rotation"
    assert (vault / "knowledge/projects/demo/journal.000001-000002.md").exists()
    assert state_body(store.render_state(live[:1], _validated=True)) == state_body(expected)


def test_a_sealed_segment_is_never_overwritten(
    vault: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    """Create, never replace: a segment that could be rewritten is not sealed."""
    monkeypatch.setattr(project_journal, "MAX_JOURNAL_EVENTS", 2)
    store = ProjectStore(vault, state_root)
    store.checkpoint("demo", checkpoint_event(), "agent-a")
    store.checkpoint("demo", checkpoint_event("evt-2", "seal:event-2"), "agent-a")
    segment = vault / "knowledge/projects/demo/journal.000001-000002.md"
    segment.parent.mkdir(parents=True, exist_ok=True)
    segment.write_bytes(b"# occupied\n")

    with pytest.raises(Exception):
        store.checkpoint("demo", checkpoint_event("evt-3", "seal:event-3"), "agent-a")

    assert segment.read_bytes() == b"# occupied\n"


def test_the_byte_bound_still_refuses_without_writing(
    vault: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    """Rotation answers a full count, not an oversized file; that bound stands."""
    monkeypatch.setattr(project_journal, "MAX_JOURNAL_BYTES", 200)
    store = ProjectStore(vault, state_root)
    journal = vault / "knowledge/projects/demo/journal.md"

    with pytest.raises(ProjectJournalReadError) as failure:
        store.checkpoint("demo", checkpoint_event(), "agent-a")

    assert failure.value.code == "too_large"
    assert not journal.exists()


def _assert_rejected_without_journal(too_small, journal_path) -> None:
    assert too_small.value.code == "too_large"
    assert not journal_path.exists()


def _assert_exact_fit_committed(exact, small_journal, exact_size) -> None:
    assert exact.sequence == 1
    assert len(small_journal.read_bytes()) == exact_size


def _assert_overflow_left_journal_intact(overflow, small_journal, before) -> None:
    assert overflow.value.code == "too_large"
    assert small_journal.read_bytes() == before


def test_prospective_byte_limit_allows_exact_size_and_rejects_overflow(
    vault: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    store = ProjectStore(vault, state_root)
    store.checkpoint("calib", checkpoint_event(), "agent-a")
    exact_size = len(
        (vault / "knowledge/projects/calib/journal.md").read_bytes()
    )

    monkeypatch.setattr(project_journal, "MAX_JOURNAL_BYTES", exact_size - 1)
    with pytest.raises(ProjectJournalReadError) as too_small:
        store.checkpoint("small", checkpoint_event(), "agent-a")
    _assert_rejected_without_journal(
        too_small, vault / "knowledge/projects/small/journal.md"
    )

    monkeypatch.setattr(project_journal, "MAX_JOURNAL_BYTES", exact_size)
    exact = store.checkpoint("small", checkpoint_event(), "agent-a")
    small_journal = vault / "knowledge/projects/small/journal.md"
    _assert_exact_fit_committed(exact, small_journal, exact_size)
    before = small_journal.read_bytes()

    with pytest.raises(ProjectJournalReadError) as overflow:
        store.checkpoint(
            "small", checkpoint_event("evt-2", "bytes:event-2"), "agent-a"
        )
    _assert_overflow_left_journal_intact(overflow, small_journal, before)


def test_existing_journal_line_is_validated_once_per_checkpoint(
    project_store: ProjectStore, monkeypatch: pytest.MonkeyPatch
):
    project_store.checkpoint("demo", checkpoint_event(), "agent-a")
    validate = project_journal.validate_schema
    existing_validations = 0

    def count_existing(instance, schema):
        nonlocal existing_validations
        if isinstance(instance, dict) and instance.get("occurrence_id") == "evt-1":
            existing_validations += 1
        return validate(instance, schema)

    monkeypatch.setattr(project_journal, "validate_schema", count_existing)

    project_store.checkpoint(
        "demo", checkpoint_event("evt-2", "single-parse:event-2"), "agent-a"
    )

    assert existing_validations == 1

