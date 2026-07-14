from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import markdown_transaction
import project_journal
import pytest
from markdown_transaction import (
    MarkdownChange,
    ProjectPendingPriorError,
    TransactionFailure,
)
from project_journal import (
    JOURNAL_HEADER,
    MAX_JOURNAL_BYTES,
    MAX_JOURNAL_EVENTS,
    ProjectFenceError,
    ProjectJournalReadError,
    ProjectJournalRebuildRequired,
    ProjectLeaseBusy,
    ProjectStore,
)
from reliable_memory import SchemaValidationError, canonical_json_bytes, validate_schema


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/projects/demo").mkdir(parents=True)
    return root


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def project_store(vault: Path, state_root: Path) -> ProjectStore:
    return ProjectStore(vault, state_root)


def checkpoint_event(
    occurrence_id: str = "evt-1",
    idempotency_key: str = "task:task-1:active",
    *,
    delta: dict[str, object] | None = None,
) -> dict[str, object]:
    complete_delta: dict[str, object] = {
        "goal": {"id": "goal-1", "action": "upsert", "value": "Ship Stage 2"},
        "phase": {"id": "phase-1", "action": "upsert", "value": "Implementation"},
        "current_task": {
            "id": "task-1",
            "action": "upsert",
            "value": "Build project journals",
        },
        "next_actions": [
            {"id": "next-1", "action": "upsert", "value": "Run recovery tests"}
        ],
        "decisions": [
            {
                "id": "decision-1",
                "action": "upsert",
                "value": "Use fenced Markdown transactions",
            }
        ],
        "blockers": [
            {"id": "blocker-1", "action": "upsert", "value": "None"}
        ],
        "changed_files": [
            {
                "id": "file-1",
                "action": "upsert",
                "value": "scripts/project_journal.py",
            }
        ],
        "commands": [
            {"id": "command-1", "action": "upsert", "value": "uv run pytest"}
        ],
        "verification": [
            {"id": "verify-1", "action": "upsert", "value": "project tests pass"}
        ],
    }
    if delta:
        complete_delta.update(delta)
    return {
        "schema_version": "project-checkpoint/v1",
        "occurrence_id": occurrence_id,
        "idempotency_key": idempotency_key,
        "provenance": {
            "agent": "agent-a",
            "session": "session-1",
            "worktree": "D:/work/wiki",
            "branch": "feature/journal",
            "source_event": "tool-42",
        },
        "trigger": "task_completed",
        "reason": "durable progress",
        "delta": complete_delta,
        "evidence_event_ids": ["tool-41", "tool-42"],
    }


def journal_records(store: ProjectStore, slug: str = "demo") -> list[dict[str, object]]:
    text = store.read_journal(slug)
    return [json.loads(line) for line in text.removeprefix(JOURNAL_HEADER).splitlines()]


def test_generated_projection_preserves_project_slug_ownership(
    project_store: ProjectStore, vault: Path, tmp_path: Path
):
    from session_start_project_state import _compute_slug

    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    event = checkpoint_event()
    event["provenance"]["worktree"] = str(project_dir)

    project_store.checkpoint("demo", event, "agent-a")

    state = vault / "knowledge/projects/demo/state.md"
    assert f"- Project root: `{project_dir}`" in state.read_text(encoding="utf-8")
    assert _compute_slug(project_dir, vault / "knowledge/projects") == "demo"


def _parse_test_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_lease_uses_random_token_monotonic_epoch_and_default_timing(
    vault: Path, state_root: Path
):
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    store = ProjectStore(vault, state_root, clock=lambda: now)

    first = store.acquire_lease("demo", "agent-a")
    assert first.expires_at == now + timedelta(seconds=30)
    assert first.heartbeat_due_at == now + timedelta(seconds=10)

    now += timedelta(seconds=31)
    second = store.acquire_lease("demo", "agent-b")
    assert second.token != first.token
    assert second.epoch == first.epoch + 1


def test_coordinator_reserves_idempotency_sequence_and_preparation_binding(
    vault: Path, state_root: Path
):
    store = ProjectStore(vault, state_root)
    lease = store.acquire_lease("demo", "agent-a")
    precondition = {
        "project": "demo",
        "lease_token": lease.token,
        "fencing_epoch": lease.epoch,
        "expires_at": lease.expires_at.isoformat().replace("+00:00", "Z"),
    }

    reservation = store.coordinator.reserve_project_checkpoint(
        "demo", checkpoint_event(), precondition
    )
    duplicate = store.coordinator.reserve_project_checkpoint(
        "demo", checkpoint_event("evt-retry"), precondition
    )
    transaction = store.coordinator.prepare(
        [
            MarkdownChange.create(
                "knowledge/projects/demo/journal.md",
                b"reserved-event\n",
            )
        ],
        operation_id=reservation.operation_id,
        preconditions={"project_lease": precondition},
        project_reservation=reservation,
    )

    assert reservation.sequence == duplicate.sequence == 1
    assert duplicate.duplicate is True
    with sqlite3.connect(store.coordinator.database_path) as database:
        row = database.execute(
            "SELECT sequence, operation_id, transaction_id, state "
            "FROM project_checkpoints WHERE project = 'demo'"
        ).fetchone()
    assert row == (1, reservation.operation_id, transaction.id, "prepared")


def test_active_lease_is_exclusive_and_heartbeat_extends_it(
    vault: Path, state_root: Path
):
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    store = ProjectStore(vault, state_root, clock=lambda: now)
    lease = store.acquire_lease("demo", "agent-a")

    with pytest.raises(ProjectLeaseBusy):
        store.acquire_lease("demo", "agent-b")

    now += timedelta(seconds=10)
    renewed = store.heartbeat(lease)
    assert renewed.expires_at == now + timedelta(seconds=30)
    assert renewed.heartbeat_due_at == now + timedelta(seconds=10)


def test_same_owner_cannot_alias_active_lease_and_exact_token_can_renew(
    vault: Path, state_root: Path
):
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    store = ProjectStore(vault, state_root, clock=lambda: now)
    lease = store.acquire_lease("demo", "agent-a")

    with pytest.raises(ProjectLeaseBusy):
        store.acquire_lease("demo", "agent-a")
    with pytest.raises(ProjectLeaseBusy):
        store.acquire_lease("demo", "agent-a", token="wrong-token")

    now += timedelta(seconds=5)
    renewed = store.acquire_lease("demo", "agent-a", token=lease.token)

    assert renewed.token == lease.token
    assert renewed.epoch == lease.epoch
    assert renewed.expires_at == now + timedelta(seconds=30)


@pytest.mark.parametrize(
    "slug",
    [
        "../demo",
        "Demo",
        "demo/name",
        "demo\\name",
        "",
        ".",
        "..",
        "con",
        "COM1.txt",
        "demo.",
        "demo ",
        "demo\x00name",
        unicodedata.normalize("NFD", "café"),
    ],
)
def test_slug_safety_is_preserved(project_store: ProjectStore, slug: str):
    with pytest.raises(ValueError):
        project_store.acquire_lease(slug, "agent-a")


@pytest.mark.parametrize("existing_state", [False, True])
def test_cyrillic_computed_slug_supports_new_and_existing_project_state(
    vault: Path,
    state_root: Path,
    tmp_path: Path,
    existing_state: bool,
):
    from session_start_project_state import _compute_slug

    project_dir = tmp_path / "Тесты"
    project_dir.mkdir()
    projects = vault / "knowledge/projects"
    slug = _compute_slug(project_dir, projects)
    assert slug == "тесты"
    if existing_state:
        target = projects / slug
        target.mkdir()
        (target / "state.md").write_text(
            f"# {slug}\n- Project root: `{project_dir}`\n", encoding="utf-8"
        )
        assert _compute_slug(project_dir, projects) == slug

    event = checkpoint_event(f"evt-{existing_state}", f"unicode:{existing_state}")
    event["provenance"]["worktree"] = str(project_dir)
    receipt = ProjectStore(vault, state_root).checkpoint(slug, event, "agent-a")

    assert receipt.project == slug
    assert (projects / slug / "journal.md").is_file()


def test_project_slug_rejects_existing_case_alias(vault: Path, state_root: Path):
    alias = vault / "knowledge/projects/CASEALIAS"
    alias.mkdir()

    with pytest.raises(ValueError, match="alias"):
        ProjectStore(vault, state_root).acquire_lease("casealias", "agent-a")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.pop("provenance"),
        lambda event: event.update(unexpected=True),
        lambda event: event["delta"]["goal"].update(action="invalid"),
    ],
)
def test_invalid_event_does_not_reserve_sequence_or_idempotency(
    project_store: ProjectStore, mutate
):
    invalid = checkpoint_event()
    mutate(invalid)

    with pytest.raises(SchemaValidationError):
        project_store.checkpoint("demo", invalid, "agent-a")

    with sqlite3.connect(project_store.coordinator.database_path) as database:
        assert database.execute("SELECT * FROM project_checkpoints").fetchall() == []
    receipt = project_store.checkpoint("demo", checkpoint_event(), "agent-a")
    assert receipt.sequence == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.update(occurrence_id="x" * 257),
        lambda event: event["delta"]["goal"].update(value="x" * 4097),
        lambda event: event["delta"].update(
            next_actions=[
                {"id": f"next-{index}", "action": "upsert", "value": "next"}
                for index in range(11)
            ]
        ),
        lambda event: event["delta"].update(
            decisions=[
                {"id": f"decision-{index}", "action": "upsert", "value": "decision"}
                for index in range(101)
            ]
        ),
        lambda event: event.update(
            evidence_event_ids=[f"evidence-{index}" for index in range(101)]
        ),
    ],
)
def test_oversized_checkpoint_fields_are_rejected_before_reservation(
    project_store: ProjectStore, mutate
):
    event = checkpoint_event()
    mutate(event)

    with pytest.raises(SchemaValidationError):
        project_store.checkpoint("demo", event, "agent-a")

    with sqlite3.connect(project_store.coordinator.database_path) as database:
        assert database.execute("SELECT * FROM project_checkpoints").fetchall() == []


def test_event_is_normalized_before_reservation(project_store: ProjectStore):
    event = checkpoint_event("e\u0301vt", "ke\u0301y")
    event["delta"]["goal"]["value"] = "Cafe\u0301"

    receipt = project_store.checkpoint("demo", event, "agent-a")
    record = journal_records(project_store)[0]

    assert receipt.occurrence_id == "\u00e9vt"
    assert receipt.idempotency_key == "k\u00e9y"
    assert record["delta"]["goal"]["value"] == "Caf\u00e9"


def test_journal_read_rejects_symlink(
    vault: Path, state_root: Path, tmp_path: Path
):
    store = ProjectStore(vault, state_root)
    journal = vault / "knowledge/projects/demo/journal.md"
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    try:
        journal.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(ProjectJournalReadError) as linked:
        store.read_journal("demo")
    assert linked.value.code == "unsafe_path"


def test_journal_read_rejects_non_regular_file(vault: Path, state_root: Path):
    journal = vault / "knowledge/projects/demo/journal.md"
    journal.mkdir()
    with pytest.raises(ProjectJournalReadError) as directory:
        ProjectStore(vault, state_root).read_journal("demo")
    assert directory.value.code == "not_regular"


@pytest.mark.skipif(os.name == "nt", reason="FIFO test requires POSIX")
def test_journal_read_rejects_fifo_without_blocking(vault: Path, state_root: Path):
    journal = vault / "knowledge/projects/demo/journal.md"
    os.mkfifo(journal)

    with pytest.raises(ProjectJournalReadError) as failure:
        ProjectStore(vault, state_root).read_journal("demo")

    assert failure.value.code == "not_regular"


def test_journal_read_and_event_count_are_bounded(vault: Path, state_root: Path):
    store = ProjectStore(vault, state_root)
    journal = vault / "knowledge/projects/demo/journal.md"
    journal.write_bytes(b"x" * (MAX_JOURNAL_BYTES + 1))

    with pytest.raises(ProjectJournalReadError) as oversized:
        store.read_journal("demo")
    assert oversized.value.code == "too_large"

    event = checkpoint_event()
    records = []
    for index in range(MAX_JOURNAL_EVENTS + 1):
        record = dict(event)
        record.update(
            occurrence_id=f"evt-{index}",
            idempotency_key=f"key-{index}",
            project="demo",
            sequence=index + 1,
            last_applied_sequence=index,
        )
        records.append(canonical_json_bytes(record))
    content = JOURNAL_HEADER.encode("utf-8") + b"\n".join(records) + b"\n"

    with pytest.raises(ProjectJournalReadError) as too_many:
        store._journal_events("demo", content)
    assert too_many.value.code == "too_many_events"


def test_journal_read_rejects_file_change_during_read(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    journal = vault / "knowledge/projects/demo/journal.md"
    journal.write_bytes(JOURNAL_HEADER.encode("utf-8"))
    fstat = project_journal.os.fstat
    calls = 0

    def changed_after_read(descriptor: int):
        nonlocal calls
        metadata = fstat(descriptor)
        calls += 1
        if calls == 2:
            values = list(metadata)
            values[6] += 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(project_journal.os, "fstat", changed_after_read)

    with pytest.raises(ProjectJournalReadError) as changed:
        ProjectStore(vault, state_root).read_journal("demo")

    assert changed.value.code == "changed"


def test_projection_read_rejects_symlink(
    vault: Path, state_root: Path, tmp_path: Path
):
    projection = vault / "knowledge/projects/demo/state.md"
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    try:
        projection.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(ProjectJournalReadError) as linked:
        ProjectStore(vault, state_root).checkpoint(
            "demo", checkpoint_event(), "agent-a"
        )

    assert linked.value.code == "unsafe_path"
    assert outside.read_text(encoding="utf-8") == "private"


def test_projection_read_rejects_non_regular_file(vault: Path, state_root: Path):
    (vault / "knowledge/projects/demo/state.md").mkdir()

    with pytest.raises(ProjectJournalReadError) as directory:
        ProjectStore(vault, state_root).checkpoint(
            "demo", checkpoint_event(), "agent-a"
        )

    assert directory.value.code == "not_regular"


@pytest.mark.skipif(os.name == "nt", reason="FIFO test requires POSIX")
def test_projection_read_rejects_fifo_without_blocking(vault: Path, state_root: Path):
    os.mkfifo(vault / "knowledge/projects/demo/state.md")

    with pytest.raises(ProjectJournalReadError) as failure:
        ProjectStore(vault, state_root).checkpoint(
            "demo", checkpoint_event(), "agent-a"
        )

    assert failure.value.code == "not_regular"


def test_projection_read_rejects_oversize_before_hash(
    vault: Path,
    state_root: Path,
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


def test_projection_read_rejects_file_change_during_read(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    projection = vault / "knowledge/projects/demo/state.md"
    projection.write_bytes(b"old projection\n")
    open_file = project_journal.os.open
    fstat = project_journal.os.fstat
    projection_descriptors: set[int] = set()
    calls: dict[int, int] = {}

    def track_open(path, flags, *args):
        descriptor = open_file(path, flags, *args)
        if Path(path) == projection:
            projection_descriptors.add(descriptor)
        return descriptor

    def changed_after_read(descriptor: int):
        metadata = fstat(descriptor)
        if descriptor not in projection_descriptors:
            return metadata
        calls[descriptor] = calls.get(descriptor, 0) + 1
        if calls[descriptor] == 2:
            values = list(metadata)
            values[6] += 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(project_journal.os, "open", track_open)
    monkeypatch.setattr(project_journal.os, "fstat", changed_after_read)

    with pytest.raises(ProjectJournalReadError) as changed:
        ProjectStore(vault, state_root).checkpoint(
            "demo", checkpoint_event(), "agent-a"
        )

    assert changed.value.code == "changed"


@pytest.mark.parametrize(
    ("name", "limit"),
    [
        ("journal.md", project_journal.MAX_JOURNAL_BYTES),
        ("state.md", project_journal.MAX_PROJECTION_BYTES),
    ],
)
def test_prepare_rejects_project_file_growth_without_new_artifact(
    vault: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    limit: int,
):
    store = ProjectStore(vault, state_root)
    store.checkpoint("demo", checkpoint_event(), "agent-a")
    artifact_ids = {path.name for path in store.coordinator.transaction_root.iterdir()}
    prepare = store.coordinator.prepare

    def grow_before_prepare(*args, **kwargs):
        (vault / "knowledge/projects/demo" / name).write_bytes(b"x" * (limit + 1))
        return prepare(*args, **kwargs)

    monkeypatch.setattr(store.coordinator, "prepare", grow_before_prepare)

    with pytest.raises(ValueError, match=f"exceeds {limit} bytes"):
        store.checkpoint(
            "demo",
            checkpoint_event("evt-2", "bounded:event-2"),
            "agent-a",
        )

    assert {
        path.name for path in store.coordinator.transaction_root.iterdir()
    } == artifact_ids


@pytest.mark.parametrize("name", ["journal.md", "state.md"])
def test_prepare_rejects_project_file_replacement_without_new_artifact(
    vault: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
):
    store = ProjectStore(vault, state_root)
    store.checkpoint("demo", checkpoint_event(), "agent-a")
    artifact_ids = {path.name for path in store.coordinator.transaction_root.iterdir()}
    prepare = store.coordinator.prepare

    def replace_before_prepare(*args, **kwargs):
        (vault / "knowledge/projects/demo" / name).write_bytes(
            b"external replacement\n"
        )
        return prepare(*args, **kwargs)

    monkeypatch.setattr(store.coordinator, "prepare", replace_before_prepare)

    with pytest.raises(ValueError, match="precondition changed before prepare"):
        store.checkpoint(
            "demo",
            checkpoint_event("evt-2", "replacement:event-2"),
            "agent-a",
        )

    assert {
        path.name for path in store.coordinator.transaction_root.iterdir()
    } == artifact_ids


def test_absent_journal_under_missing_project_directory_is_valid(
    vault: Path, state_root: Path
):
    project = vault / "knowledge/projects/new-project"
    assert not project.exists()
    store = ProjectStore(vault, state_root)

    assert store.read_journal("new-project") == ""
    receipt = store.checkpoint("new-project", checkpoint_event(), "agent-a")

    assert receipt.sequence == 1
    assert (project / "journal.md").is_file()
    assert (project / "state.md").is_file()


def test_prospective_event_count_allows_limit_and_rejects_next_without_writes(
    vault: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(project_journal, "MAX_JOURNAL_EVENTS", 2)
    store = ProjectStore(vault, state_root)

    first = store.checkpoint("demo", checkpoint_event(), "agent-a")
    second = store.checkpoint(
        "demo", checkpoint_event("evt-2", "limit:event-2"), "agent-a"
    )
    journal = vault / "knowledge/projects/demo/journal.md"
    projection = vault / "knowledge/projects/demo/state.md"
    before = (journal.read_bytes(), projection.read_bytes())

    with pytest.raises(ProjectJournalReadError) as failure:
        store.checkpoint(
            "demo", checkpoint_event("evt-3", "limit:event-3"), "agent-a"
        )

    assert [first.sequence, second.sequence] == [1, 2]
    assert failure.value.code == "too_many_events"
    assert (journal.read_bytes(), projection.read_bytes()) == before


def test_prospective_byte_limit_allows_exact_size_and_rejects_overflow(
    vault: Path,
    state_root: Path,
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
    assert too_small.value.code == "too_large"
    assert not (vault / "knowledge/projects/small/journal.md").exists()

    monkeypatch.setattr(project_journal, "MAX_JOURNAL_BYTES", exact_size)
    exact = store.checkpoint("small", checkpoint_event(), "agent-a")
    assert exact.sequence == 1
    small_journal = vault / "knowledge/projects/small/journal.md"
    assert len(small_journal.read_bytes()) == exact_size
    before = small_journal.read_bytes()

    with pytest.raises(ProjectJournalReadError) as overflow:
        store.checkpoint(
            "small", checkpoint_event("evt-2", "bytes:event-2"), "agent-a"
        )
    assert overflow.value.code == "too_large"
    assert small_journal.read_bytes() == before


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


def test_slow_checkpoint_phases_renew_lease_before_apply(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    now = datetime.now(timezone.utc)
    store = ProjectStore(vault, state_root, clock=lambda: now)
    heartbeat = store.heartbeat
    heartbeats: list[datetime] = []

    def record_heartbeat(lease, *args, **kwargs):
        heartbeats.append(now)
        return heartbeat(lease, *args, **kwargs)

    def slow(method):
        def wrapped(*args, **kwargs):
            nonlocal now
            result = method(*args, **kwargs)
            now += timedelta(seconds=11)
            return result

        return wrapped

    monkeypatch.setattr(store, "heartbeat", record_heartbeat)
    monkeypatch.setattr(store, "_read_journal_bytes", slow(store._read_journal_bytes))
    monkeypatch.setattr(store, "render_state", slow(store.render_state))
    monkeypatch.setattr(store.coordinator, "prepare", slow(store.coordinator.prepare))

    receipt = store.checkpoint("demo", checkpoint_event(), "agent-a")

    assert receipt.sequence == 1
    assert len(heartbeats) >= 7
    with sqlite3.connect(store.coordinator.database_path) as database:
        assert database.execute(
            "SELECT state FROM project_checkpoints WHERE project = 'demo'"
        ).fetchone()[0] == "committed"


def test_checkpoint_is_append_only_idempotent_and_projects_state(
    project_store: ProjectStore,
):
    first = project_store.checkpoint("demo", checkpoint_event(), "agent-a")
    before = project_store.read_journal("demo")
    duplicate = project_store.checkpoint("demo", checkpoint_event(), "agent-a")
    second = project_store.checkpoint(
        "demo",
        checkpoint_event("evt-2", "blocker:blocker-1:closed", delta={
            "blockers": [
                {"id": "blocker-1", "action": "close", "value": "resolved"}
            ],
            "current_task": {
                "id": "task-1",
                "action": "upsert",
                "value": "Review journal recovery",
            },
        }),
        "agent-a",
    )

    assert first.sequence == duplicate.sequence == 1
    assert duplicate.duplicate is True
    assert second.sequence == 2
    assert project_store.read_journal("demo").startswith(before)
    records = journal_records(project_store)
    assert [record["sequence"] for record in records] == [1, 2]
    assert records[0]["last_applied_sequence"] == 0
    assert records[1]["last_applied_sequence"] == 1
    validate_schema(records[0], Path("scripts/schemas/project-checkpoint-v1.json"))

    state = (project_store.vault / "knowledge/projects/demo/state.md").read_text(
        encoding="utf-8"
    )
    assert "generated: true" in state
    assert "last_applied_sequence: 2" in state
    assert "Review journal recovery" in state
    assert "blocker-1" not in state
    assert "Use fenced Markdown transactions" in state


def test_current_task_transition_list_replays_close_open_close_without_resurrection(
    project_store: ProjectStore,
):
    project_store.checkpoint("demo", checkpoint_event(), "agent-a")
    transition = checkpoint_event(
        "evt-2",
        "task-transition",
        delta={
            "current_task": {
                "id": "task-2",
                "action": "close",
                "value": "New task complete",
            },
            "current_task_operations": [
                {"id": "task-1", "action": "close", "value": "Old task complete"},
                {"id": "task-2", "action": "upsert", "value": "New task"},
                {"id": "task-2", "action": "close", "value": "New task complete"},
            ],
        },
    )

    project_store.checkpoint("demo", transition, "agent-a")

    assert project_store.projection("demo").current_task == {}
    state = (project_store.vault / "knowledge/projects/demo/state.md").read_text(
        encoding="utf-8"
    )
    assert "## Current task\n- None" in state


def test_goal_and_phase_transition_lists_do_not_resurrect_closed_values(
    project_store: ProjectStore,
):
    project_store.checkpoint("demo", checkpoint_event(), "agent-a")
    transition = checkpoint_event(
        "evt-2",
        "goal-phase-transition",
        delta={
            "goal": {"id": "goal-2", "action": "close", "value": "done"},
            "phase": {"id": "phase-2", "action": "close", "value": "done"},
            "goal_operations": [
                {"id": "goal-1", "action": "close", "value": "old done"},
                {"id": "goal-2", "action": "upsert", "value": "new"},
                {"id": "goal-2", "action": "close", "value": "new done"},
            ],
            "phase_operations": [
                {"id": "phase-1", "action": "close", "value": "old done"},
                {"id": "phase-2", "action": "upsert", "value": "new"},
                {"id": "phase-2", "action": "close", "value": "new done"},
            ],
        },
    )

    project_store.checkpoint("demo", transition, "agent-a")

    projection = project_store.projection("demo")
    assert projection.goal == {}
    assert projection.phase == {}


def test_owned_legacy_state_is_bootstrapped_before_first_empty_checkpoint(
    project_store: ProjectStore, vault: Path, tmp_path: Path
):
    project_root = tmp_path / "legacy-project"
    project_root.mkdir()
    state = vault / "knowledge/projects/demo/state.md"
    state.write_text(
        "---\ntype: project-state\ntitle: Legacy\nproject: demo\ngenerated: true\n---\n"
        "# Legacy\n\n## Source\n"
        f"- Project root: `{project_root}`\n\n"
        "## Goal\n- Ship legacy goal\n\n"
        "## Current task\n- Preserve legacy task\n\n"
        "## Next actions\n- Run legacy tests\n\n"
        "## Recent decisions\n- Keep legacy design\n\n"
        "## Open blockers\n- Waiting on legacy input\n",
        encoding="utf-8",
    )
    event = checkpoint_event(
        "session-end-1",
        "session-end-1",
        delta={
            "goal": {"id": "checkpoint-none", "action": "close", "value": ""},
            "phase": {"id": "checkpoint-none", "action": "close", "value": ""},
            "current_task": {"id": "checkpoint-none", "action": "close", "value": ""},
            "next_actions": [],
            "decisions": [],
            "blockers": [],
            "changed_files": [],
            "commands": [],
            "verification": [],
        },
    )
    event["provenance"]["worktree"] = str(project_root)

    project_store.checkpoint("demo", event, "agent-a")
    restarted = ProjectStore(vault, project_store.state_root)
    restarted.checkpoint("demo", event, "agent-a")

    records = journal_records(restarted)
    assert [record["reason"] for record in records] == ["bootstrap_legacy_state", "durable progress"]
    rendered = state.read_text(encoding="utf-8")
    for value in (
        "Ship legacy goal",
        "Preserve legacy task",
        "Run legacy tests",
        "Keep legacy design",
        "Waiting on legacy input",
        f"- Project root: `{project_root}`",
    ):
        assert value in rendered


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        (
            "project-state-current.md",
            [
                "Preserve current goal",
                "Preserve current phase",
                "Preserve current task",
                "Preserve current next action",
                "Preserve current decision",
                "Preserve current blocker",
                "Preserve changed file",
                "Preserve command",
                "Preserve verification",
            ],
        ),
        (
            "project-state-older.md",
            [
                "Preserve older summary",
                "Preserve older handoff",
                "Preserve older stopping point",
                "Preserve older decision",
                "Preserve older open thread",
                "Preserve Older Link",
                "https://example.test/demo.git",
                "Preserve older editorial context",
            ],
        ),
    ],
)
def test_shipped_legacy_state_fixtures_preserve_every_nonempty_section(
    project_store: ProjectStore,
    vault: Path,
    tmp_path: Path,
    fixture_name: str,
    expected: list[str],
):
    project_root = tmp_path / "legacy-fixture-project"
    project_root.mkdir()
    fixture = Path(__file__).parent / "fixtures" / fixture_name
    source = fixture.read_text(encoding="utf-8").replace(
        "{PROJECT_ROOT}", str(project_root)
    )
    state_path = vault / "knowledge/projects/demo/state.md"
    state_path.write_text(source, encoding="utf-8")
    event = checkpoint_event(
        "session-end-fixture",
        "session-end-fixture",
        delta={
            "goal": {"id": "checkpoint-none", "action": "close", "value": ""},
            "phase": {"id": "checkpoint-none", "action": "close", "value": ""},
            "current_task": {"id": "checkpoint-none", "action": "close", "value": ""},
            "next_actions": [],
            "decisions": [],
            "blockers": [],
            "changed_files": [],
            "commands": [],
            "verification": [],
        },
    )
    event["provenance"]["worktree"] = str(project_root)

    project_store.checkpoint("demo", event, "agent-a")

    rendered = state_path.read_text(encoding="utf-8")
    for value in expected:
        assert value in rendered


def test_bootstrap_identity_hashes_256_character_unicode_slug():
    from project_journal import _bootstrap_event_identity

    slug = "я" * 256
    occurrence_id, idempotency_key, stable_hash = _bootstrap_event_identity(
        slug, "meaningful legacy content"
    )

    assert slug not in occurrence_id
    assert slug not in idempotency_key
    assert len(occurrence_id) <= 256
    assert len(idempotency_key) <= 256
    assert len(stable_hash) == 64
    assert (occurrence_id, idempotency_key, stable_hash) == _bootstrap_event_identity(
        slug, "meaningful legacy content"
    )


def test_idempotency_key_deduplicates_a_new_occurrence(project_store: ProjectStore):
    first = project_store.checkpoint("demo", checkpoint_event(), "agent-a")
    duplicate = project_store.checkpoint(
        "demo", checkpoint_event("evt-retried"), "agent-a"
    )

    assert (first.sequence, duplicate.sequence, duplicate.duplicate) == (1, 1, True)
    assert len(journal_records(project_store)) == 1


def test_idempotency_key_rejects_changed_payload(project_store: ProjectStore):
    project_store.checkpoint("demo", checkpoint_event(), "agent-a")

    with pytest.raises(ValueError, match="idempotency_key"):
        project_store.checkpoint(
            "demo",
            checkpoint_event(
                "evt-retried",
                delta={
                    "current_task": {
                        "id": "task-1",
                        "action": "upsert",
                        "value": "A different operation",
                    }
                },
            ),
            "agent-a",
        )


def test_conflicting_occurrence_id_is_rejected(project_store: ProjectStore):
    project_store.checkpoint("demo", checkpoint_event(), "agent-a")

    with pytest.raises(ValueError, match="occurrence_id"):
        project_store.checkpoint(
            "demo",
            checkpoint_event("evt-1", "different-key"),
            "agent-a",
        )


def test_render_state_is_deterministic_and_bounded(project_store: ProjectStore):
    events = []
    for index in range(20):
        event = checkpoint_event(f"evt-{index}", f"decision-{index}")
        event.update(project="demo", sequence=index + 1, last_applied_sequence=index)
        event["delta"]["decisions"] = [
            {
                "id": f"decision-{index}",
                "action": "upsert",
                "value": "x" * 1000,
            }
        ]
        for section in (
            "next_actions",
            "blockers",
            "changed_files",
            "commands",
            "verification",
        ):
            event["delta"][section] = [
                {
                    "id": f"{section}-{index}",
                    "action": "upsert",
                    "value": "x" * 1000,
                }
            ]
        events.append(event)

    first = project_store.render_state(events)
    second = project_store.render_state(list(events))

    assert first == second
    assert len(first) <= 12_000
    assert first.count(b"- `decision-") <= 5
    assert b"last_applied_sequence: 20" in first


def test_final_apply_rechecks_lease_and_rejects_stale_epoch(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)
    real_apply = store.coordinator.apply

    def steal_then_apply(transaction_id: str):
        with sqlite3.connect(store.coordinator.database_path) as database:
            database.execute(
                "UPDATE project_leases SET lease_token = 'new-token', "
                "fencing_epoch = fencing_epoch + 1 WHERE project = 'demo'"
            )
            database.commit()
        return real_apply(transaction_id)

    monkeypatch.setattr(store.coordinator, "apply", steal_then_apply)

    with pytest.raises(ProjectFenceError):
        store.checkpoint("demo", checkpoint_event(), "agent-a")
    assert not (vault / "knowledge/projects/demo/journal.md").exists()
    assert not (vault / "knowledge/projects/demo/state.md").exists()


def test_fenced_reservation_blocks_later_sequence_until_replayed(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)
    real_apply = store.coordinator.apply

    def steal_then_apply(transaction_id: str):
        with sqlite3.connect(store.coordinator.database_path) as database:
            database.execute(
                "UPDATE project_leases SET lease_token = 'new-token', "
                "fencing_epoch = fencing_epoch + 1 WHERE project = 'demo'"
            )
            database.commit()
        return real_apply(transaction_id)

    monkeypatch.setattr(store.coordinator, "apply", steal_then_apply)
    with pytest.raises(ProjectFenceError):
        store.checkpoint("demo", checkpoint_event(), "agent-a")
    monkeypatch.setattr(store.coordinator, "apply", real_apply)
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE project = 'demo'"
        )
        database.commit()

    with pytest.raises(ProjectPendingPriorError):
        store.checkpoint(
            "demo", checkpoint_event("evt-2", "event-after-fence"), "agent-a"
        )

    first = store.checkpoint("demo", checkpoint_event(), "agent-a")
    second = store.checkpoint(
        "demo", checkpoint_event("evt-2", "event-after-fence"), "agent-a"
    )
    events = journal_records(store)

    assert [first.sequence, second.sequence] == [1, 2]
    assert [event["sequence"] for event in events] == [1, 2]
    assert [event["last_applied_sequence"] for event in events] == [0, 0]


def test_released_prepared_checkpoint_requires_forward_replay(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)
    transaction_id = ""

    def crash_before_apply(candidate: str):
        nonlocal transaction_id
        transaction_id = candidate
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(store.coordinator, "apply", crash_before_apply)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.checkpoint("demo", checkpoint_event(), "agent-a")

    recovered = ProjectStore(vault, state_root).recover("demo")

    assert transaction_id
    assert recovered == []
    assert not (vault / "knowledge/projects/demo/journal.md").exists()

    replayed = ProjectStore(vault, state_root).checkpoint(
        "demo", checkpoint_event(), "agent-b"
    )

    assert replayed.sequence == 1
    assert len(journal_records(ProjectStore(vault, state_root))) == 1
    assert (vault / "knowledge/projects/demo/state.md").exists()


def test_new_epoch_wins_before_recovery_and_old_checkpoint_touches_nothing(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)

    def crash_before_apply(transaction_id: str):
        raise RuntimeError(f"crash before {transaction_id}")

    monkeypatch.setattr(store.coordinator, "apply", crash_before_apply)
    with pytest.raises(RuntimeError, match="crash before"):
        store.checkpoint("demo", checkpoint_event(), "agent-a")
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET lease_token = 'new-token', fencing_epoch = 2 "
            "WHERE project = 'demo'"
        )
        database.commit()

    assert ProjectStore(vault, state_root).recover("demo") == []
    assert not (vault / "knowledge/projects/demo/journal.md").exists()
    assert not (vault / "knowledge/projects/demo/state.md").exists()
    with sqlite3.connect(store.coordinator.database_path) as database:
        state = database.execute(
            "SELECT state FROM project_checkpoints WHERE project = 'demo'"
        ).fetchone()[0]
    assert state == "quarantined"


def test_expired_prepared_checkpoint_replays_same_reservation_with_new_attempt(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)
    event = checkpoint_event()

    def crash_before_apply(transaction_id: str):
        raise RuntimeError(f"crash before {transaction_id}")

    monkeypatch.setattr(store.coordinator, "apply", crash_before_apply)
    with pytest.raises(RuntimeError, match="crash before"):
        store.checkpoint("demo", event, "agent-a")
    with sqlite3.connect(store.coordinator.database_path) as database:
        first_operation = database.execute(
            "SELECT operation_id FROM project_checkpoints WHERE project = 'demo'"
        ).fetchone()[0]
        database.execute(
            "UPDATE project_leases SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE project = 'demo'"
        )
        database.commit()

    replay_store = ProjectStore(vault, state_root)
    receipt = replay_store.checkpoint("demo", event, "agent-b")
    duplicate = replay_store.checkpoint("demo", event, "agent-b")

    assert receipt.sequence == duplicate.sequence == 1
    assert duplicate.duplicate is True
    assert receipt.transaction_id == duplicate.transaction_id
    assert replay_store.read_journal("demo").count('"occurrence_id":"evt-1"') == 1
    assert (vault / "knowledge/projects/demo/state.md").exists()
    with sqlite3.connect(replay_store.coordinator.database_path) as database:
        attempts = database.execute(
            "SELECT attempt_number, operation_id, parent_operation_id, transaction_id, state "
            "FROM project_checkpoint_attempts WHERE project = 'demo' "
            "ORDER BY attempt_number"
        ).fetchall()
        current = database.execute(
            "SELECT sequence, attempt_number, operation_id, transaction_id, state "
            "FROM project_checkpoints WHERE project = 'demo'"
        ).fetchone()
    assert len(attempts) == 2
    assert attempts[0][0] == 1
    assert attempts[0][1] == first_operation
    assert attempts[0][2] is None
    assert attempts[0][4] == "quarantined"
    assert attempts[1][0] == 2
    assert attempts[1][1] != first_operation
    assert attempts[1][2] == first_operation
    assert attempts[1][3] == receipt.transaction_id
    assert attempts[1][4] == "committed"
    assert current == (1, 2, attempts[1][1], receipt.transaction_id, "committed")


def test_concurrent_duplicate_replay_creates_one_forward_attempt(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)
    event = checkpoint_event()

    def crash_before_apply(transaction_id: str):
        raise RuntimeError(f"crash before {transaction_id}")

    monkeypatch.setattr(store.coordinator, "apply", crash_before_apply)
    with pytest.raises(RuntimeError, match="crash before"):
        store.checkpoint("demo", event, "agent-a")
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE project = 'demo'"
        )
        database.commit()
    barrier = threading.Barrier(2)

    def replay(owner: str):
        replay_store = ProjectStore(vault, state_root)
        barrier.wait()
        for _ in range(100):
            try:
                return replay_store.checkpoint("demo", event, owner)
            except ProjectLeaseBusy:
                time.sleep(0.01)
        raise AssertionError("replay lease was never released")

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(replay, ("agent-b", "agent-c")))

    assert [receipt.sequence for receipt in receipts] == [1, 1]
    assert sorted(receipt.duplicate for receipt in receipts) == [False, True]
    assert len({receipt.transaction_id for receipt in receipts}) == 1
    replay_store = ProjectStore(vault, state_root)
    assert replay_store.read_journal("demo").count('"occurrence_id":"evt-1"') == 1
    with sqlite3.connect(replay_store.coordinator.database_path) as database:
        attempts = database.execute(
            "SELECT attempt_number, state FROM project_checkpoint_attempts "
            "WHERE project = 'demo' ORDER BY attempt_number"
        ).fetchall()
    assert attempts == [(1, "quarantined"), (2, "committed")]


def test_newer_sequence_waits_for_quarantined_prior_then_appends_in_order(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)
    first_event = checkpoint_event("evt-first", "head:first")
    second_event = checkpoint_event("evt-second", "head:second")

    def crash_before_apply(transaction_id: str):
        raise RuntimeError(f"crash before {transaction_id}")

    monkeypatch.setattr(store.coordinator, "apply", crash_before_apply)
    with pytest.raises(RuntimeError, match="crash before"):
        store.checkpoint("demo", first_event, "agent-a")
    monkeypatch.undo()
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE project = 'demo'"
        )
        database.commit()

    with pytest.raises(ProjectPendingPriorError) as blocked:
        ProjectStore(vault, state_root).checkpoint("demo", second_event, "agent-b")

    assert blocked.value.code == "pending_prior"
    assert blocked.value.prior_sequence == 1
    assert not (vault / "knowledge/projects/demo/journal.md").exists()
    with sqlite3.connect(store.coordinator.database_path) as database:
        rows = database.execute(
            "SELECT sequence, state FROM project_checkpoints "
            "WHERE project = 'demo' ORDER BY sequence"
        ).fetchall()
    assert rows == [(1, "quarantined"), (2, "reserved")]

    first = ProjectStore(vault, state_root).checkpoint("demo", first_event, "agent-c")
    second = ProjectStore(vault, state_root).checkpoint("demo", second_event, "agent-d")

    assert [first.sequence, second.sequence] == [1, 2]
    assert [record["sequence"] for record in journal_records(ProjectStore(vault, state_root))] == [
        1,
        2,
    ]


def test_concurrent_newer_sequence_retries_after_crashed_head(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)
    first_event = checkpoint_event("evt-first", "race:first")
    second_event = checkpoint_event("evt-second", "race:second")

    def crash_before_apply(transaction_id: str):
        raise RuntimeError(f"crash before {transaction_id}")

    monkeypatch.setattr(store.coordinator, "apply", crash_before_apply)
    with pytest.raises(RuntimeError, match="crash before"):
        store.checkpoint("demo", first_event, "agent-a")
    monkeypatch.undo()
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE project = 'demo'"
        )
        database.commit()
    newer_blocked = threading.Event()
    head_committed = threading.Event()

    def replay_newer():
        newer_store = ProjectStore(vault, state_root)
        with pytest.raises(ProjectPendingPriorError):
            newer_store.checkpoint("demo", second_event, "agent-b")
        newer_blocked.set()
        assert head_committed.wait(5)
        return newer_store.checkpoint("demo", second_event, "agent-b")

    def replay_head():
        assert newer_blocked.wait(5)
        receipt = ProjectStore(vault, state_root).checkpoint(
            "demo", first_event, "agent-c"
        )
        head_committed.set()
        return receipt

    with ThreadPoolExecutor(max_workers=2) as pool:
        newer = pool.submit(replay_newer)
        head = pool.submit(replay_head)
        receipts = [head.result(timeout=10), newer.result(timeout=10)]

    assert [receipt.sequence for receipt in receipts] == [1, 2]
    assert [record["occurrence_id"] for record in journal_records(ProjectStore(vault, state_root))] == [
        "evt-first",
        "evt-second",
    ]


def test_legacy_out_of_order_journal_is_quarantined_without_rewriting_bytes(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)
    first_event = checkpoint_event("evt-first", "legacy:first")
    second_event = checkpoint_event("evt-second", "legacy:second")

    def crash_before_apply(transaction_id: str):
        raise RuntimeError(f"crash before {transaction_id}")

    monkeypatch.setattr(store.coordinator, "apply", crash_before_apply)
    with pytest.raises(RuntimeError, match="crash before"):
        store.checkpoint("demo", first_event, "agent-a")
    monkeypatch.undo()
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE project = 'demo'"
        )
        database.commit()
    with pytest.raises(ProjectPendingPriorError):
        ProjectStore(vault, state_root).checkpoint("demo", second_event, "agent-b")

    with sqlite3.connect(store.coordinator.database_path) as database:
        second_json = database.execute(
            "SELECT event_json FROM project_checkpoints "
            "WHERE project = 'demo' AND sequence = 2"
        ).fetchone()[0]
        database.execute(
            "UPDATE project_checkpoints SET state = 'committed' "
            "WHERE project = 'demo' AND sequence = 2"
        )
        database.execute(
            "UPDATE project_checkpoint_attempts SET state = 'committed' "
            "WHERE project = 'demo' AND sequence = 2"
        )
        database.commit()
    journal = vault / "knowledge/projects/demo/journal.md"
    projection = vault / "knowledge/projects/demo/state.md"
    legacy_journal = JOURNAL_HEADER.encode("utf-8") + second_json.encode("utf-8") + b"\n"
    journal.write_bytes(legacy_journal)
    projection.write_bytes(b"legacy projection\n")

    with pytest.raises(ProjectJournalRebuildRequired) as blocked:
        ProjectStore(vault, state_root).checkpoint("demo", first_event, "agent-c")

    assert blocked.value.status == "journal_rebuild_required"
    assert journal.read_bytes() == legacy_journal
    assert projection.read_bytes() == b"legacy projection\n"
    with sqlite3.connect(store.coordinator.database_path) as database:
        first_state = database.execute(
            "SELECT state FROM project_checkpoints "
            "WHERE project = 'demo' AND sequence = 1"
        ).fetchone()[0]
    assert first_state == "quarantined"


def test_recover_replays_reservation_left_before_prepare(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)

    def crash_before_prepare(*args, **kwargs):
        raise RuntimeError("simulated pre-prepare crash")

    monkeypatch.setattr(store.coordinator, "prepare", crash_before_prepare)
    with pytest.raises(RuntimeError, match="pre-prepare"):
        store.checkpoint("demo", checkpoint_event(), "agent-a")
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE project = 'demo'"
        )
        database.commit()

    recovered = ProjectStore(vault, state_root).recover("demo")

    assert [receipt.sequence for receipt in recovered] == [1]
    assert len(journal_records(ProjectStore(vault, state_root))) == 1


def test_simultaneous_projectors_append_once_per_event(vault: Path, state_root: Path):
    barrier = threading.Barrier(2)

    def write(index: int):
        store = ProjectStore(vault, state_root)
        barrier.wait()
        for _ in range(100):
            try:
                return store.checkpoint(
                    "demo",
                    checkpoint_event(f"evt-{index}", f"event-{index}"),
                    f"agent-{index}",
                )
            except ProjectLeaseBusy:
                continue
        raise AssertionError("project lease never became available")

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(write, range(2)))

    assert sorted(receipt.sequence for receipt in receipts) == [1, 2]
    records = journal_records(ProjectStore(vault, state_root))
    assert {record["occurrence_id"] for record in records} == {"evt-0", "evt-1"}
    assert [record["sequence"] for record in records] == [1, 2]
    assert all(
        canonical_json_bytes(record).decode()
        in ProjectStore(vault, state_root).read_journal("demo")
        for record in records
    )


def test_same_owner_simultaneous_projectors_retry_without_sharing_lease(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    first_store = ProjectStore(vault, state_root)
    second_store = ProjectStore(vault, state_root)
    first_reserved = threading.Event()
    release_first = threading.Event()
    project = first_store._project_reserved

    def pause_first(reservation, lease):
        first_reserved.set()
        assert release_first.wait(5)
        return project(reservation, lease)

    monkeypatch.setattr(first_store, "_project_reserved", pause_first)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            first_store.checkpoint,
            "demo",
            checkpoint_event("evt-first", "same-owner:first"),
            "agent-a",
        )
        assert first_reserved.wait(5)
        with pytest.raises(ProjectLeaseBusy):
            second_store.checkpoint(
                "demo",
                checkpoint_event("evt-second", "same-owner:second"),
                "agent-a",
            )
        release_first.set()
        first_receipt = first.result(timeout=5)

    second_receipt = second_store.checkpoint(
        "demo",
        checkpoint_event("evt-second", "same-owner:second"),
        "agent-a",
    )

    assert [first_receipt.sequence, second_receipt.sequence] == [1, 2]
    assert [record["occurrence_id"] for record in journal_records(second_store)] == [
        "evt-first",
        "evt-second",
    ]


def test_checkpoint_failure_releases_lease_for_immediate_retry(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    failing = ProjectStore(vault, state_root)

    def fail_after_reservation(*args, **kwargs):
        raise RuntimeError("injected checkpoint failure")

    monkeypatch.setattr(failing, "_project_reserved", fail_after_reservation)
    with pytest.raises(RuntimeError, match="injected checkpoint failure"):
        failing.checkpoint("demo", checkpoint_event(), "agent-a")

    retried = ProjectStore(vault, state_root).checkpoint(
        "demo", checkpoint_event(), "agent-b"
    )
    assert retried.sequence == 1
    assert (vault / "knowledge/projects/demo/journal.md").exists()


@pytest.mark.parametrize("phase", ["prepare", "apply"])
def test_prepare_and_apply_failures_release_lease_for_retry(
    vault: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
):
    failing = ProjectStore(vault, state_root)

    def injected_failure(*args, **kwargs):
        raise RuntimeError(f"injected {phase} failure")

    monkeypatch.setattr(failing.coordinator, phase, injected_failure)
    with pytest.raises(RuntimeError, match=f"injected {phase} failure"):
        failing.checkpoint("demo", checkpoint_event(), "agent-a")
    monkeypatch.undo()

    retried = ProjectStore(vault, state_root).checkpoint(
        "demo", checkpoint_event(), "agent-b"
    )

    assert retried.sequence == 1
    assert (vault / "knowledge/projects/demo/journal.md").exists()


def test_recover_failure_releases_lease_for_immediate_retry(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)

    def fail_prepare(*args, **kwargs):
        raise RuntimeError("leave reservation")

    monkeypatch.setattr(store.coordinator, "prepare", fail_prepare)
    with pytest.raises(RuntimeError, match="leave reservation"):
        store.checkpoint("demo", checkpoint_event(), "agent-a")
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE project = 'demo'"
        )
        database.commit()
    recovering = ProjectStore(vault, state_root)

    def fail_replay(*args, **kwargs):
        raise RuntimeError("injected recovery failure")

    monkeypatch.setattr(recovering, "_project_reserved", fail_replay)
    with pytest.raises(RuntimeError, match="injected recovery failure"):
        recovering.recover("demo")

    replacement = ProjectStore(vault, state_root).acquire_lease("demo", "agent-b")
    assert replacement.owner == "agent-b"


def test_releasing_stale_token_never_releases_successor(
    vault: Path, state_root: Path
):
    store = ProjectStore(vault, state_root)
    stale = store.acquire_lease("demo", "agent-a")
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE project = 'demo'"
        )
        database.commit()
    successor = store.acquire_lease("demo", "agent-b")

    store._release(stale)

    with pytest.raises(ProjectLeaseBusy):
        store.acquire_lease("demo", "agent-c")
    with sqlite3.connect(store.coordinator.database_path) as database:
        token = database.execute(
            "SELECT lease_token FROM project_leases WHERE project = 'demo'"
        ).fetchone()[0]
    assert token == successor.token


def test_successful_checkpoint_releases_lease_once(
    project_store: ProjectStore, monkeypatch: pytest.MonkeyPatch
):
    release = project_store._release
    released: list[str] = []

    def count_release(lease):
        released.append(lease.token)
        return release(lease)

    monkeypatch.setattr(project_store, "_release", count_release)

    project_store.checkpoint("demo", checkpoint_event(), "agent-a")

    assert len(released) == 1


def test_post_prepare_heartbeat_refreshes_persisted_lease_precondition(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    now = datetime.now(timezone.utc)
    store = ProjectStore(vault, state_root, clock=lambda: now)
    real_prepare = store.coordinator.prepare
    initial_expiry = ""

    def slow_prepare(*args, **kwargs):
        nonlocal now, initial_expiry
        initial_expiry = kwargs["preconditions"]["project_lease"]["expires_at"]
        transaction = real_prepare(*args, **kwargs)
        now += timedelta(seconds=20)
        return transaction

    real_refresh = store.coordinator.refresh_project_lease_precondition
    refreshes = 0

    def advance_after_first_refresh(transaction_id, lease):
        nonlocal now, refreshes
        result = real_refresh(transaction_id, lease)
        refreshes += 1
        if refreshes == 1:
            now += timedelta(seconds=15)
        return result

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(markdown_transaction, "datetime", FakeDateTime)
    monkeypatch.setattr(store.coordinator, "prepare", slow_prepare)
    monkeypatch.setattr(
        store.coordinator,
        "refresh_project_lease_precondition",
        advance_after_first_refresh,
    )

    receipt = store.checkpoint("demo", checkpoint_event(), "agent-a")

    assert receipt.sequence == 1
    assert refreshes == 2
    assert _parse_test_timestamp(initial_expiry) < now
    with sqlite3.connect(store.coordinator.database_path) as database:
        persisted = json.loads(
            database.execute(
                'SELECT preconditions_json FROM "transaction" WHERE id = ?',
                (receipt.transaction_id,),
            ).fetchone()[0]
        )
    assert _parse_test_timestamp(persisted["project_lease"]["expires_at"]) > now


def test_prepared_lease_refresh_rejects_successor_token(
    vault: Path, state_root: Path
):
    store = ProjectStore(vault, state_root)
    lease = store.acquire_lease("demo", "agent-a")
    reservation, _ = store._reserve("demo", checkpoint_event(), lease)
    store._ensure_project_directory("demo")
    precondition = {
        "project": "demo",
        "lease_token": lease.token,
        "fencing_epoch": lease.epoch,
        "expires_at": lease.expires_at.isoformat().replace("+00:00", "Z"),
    }
    transaction = store.coordinator.prepare(
        [MarkdownChange.create("knowledge/projects/demo/journal.md", b"event\n")],
        operation_id=reservation.operation_id,
        preconditions={"project_lease": precondition},
        project_reservation=reservation,
    )
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET lease_token = 'successor', fencing_epoch = 2 "
            "WHERE project = 'demo'"
        )
        database.commit()

    with pytest.raises(TransactionFailure) as rejected:
        store.coordinator.refresh_project_lease_precondition(
            transaction.id,
            {
                "project": "demo",
                "lease_token": "successor",
                "fencing_epoch": 2,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=1)
                ).isoformat().replace("+00:00", "Z"),
            },
        )

    assert rejected.value.code == "precondition_failed"
    assert store.coordinator._record(transaction.id).state == "prepared"
