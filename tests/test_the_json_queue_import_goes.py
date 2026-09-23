"""The JSON queue of v3.3.0–v3.4.0 is refused and named, never imported.

Measured on 2026-09-23: the installer's adoption already refused a vault holding
`run/queue/` records and never ran the import, so the import was a path only a v2
client, `doctor --repair` or `memory_queue.py migrate` could reach on an unadopted
vault. See `docs/research/2026-09-23-the-json-queue-import-goes.md`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import archive_daily  # noqa: E402
import doctor  # noqa: E402
import memory_queue  # noqa: E402
from memory_queue import MemoryQueue, QueueOperationError  # noqa: E402

from tests.test_doctor import _build_root, _check  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def _json_queue(state_root: Path, *names: str) -> Path:
    queue = state_root / "run" / "queue"
    queue.mkdir(parents=True)
    for name in names:
        (queue / name).write_text(json.dumps({"id": name, "payload": {}}), encoding="utf-8")
    return queue


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def test_the_importer_its_marker_and_the_migrate_command_are_gone() -> None:
    removed = ("migrate_legacy_queue", "LegacyBackendDisabled", "MigrationReceipt", "_cli_migrate")
    assert [name for name in removed if hasattr(memory_queue, name)] == []
    assert "migrate" not in memory_queue._CLI_COMMANDS
    assert not hasattr(archive_daily.DailyArchiver, "_legacy_queue_references")
    assert not hasattr(doctor, "_repair_leases")


def test_a_json_queue_holding_records_refuses_the_v2_queue(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    _json_queue(state_root, "legacy-1.json")

    with pytest.raises(QueueOperationError) as raised:
        memory_queue.active_or_legacy_memory_queue(_vault(tmp_path), state_root)

    assert raised.value.code == "legacy_json_queue_unsupported"
    assert not (state_root / "run" / "queue.sqlite3").exists()
    assert (state_root / "run" / "queue" / "legacy-1.json").exists()


def test_an_empty_json_queue_directory_is_not_a_refusal(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    _json_queue(state_root)

    queue = memory_queue.active_or_legacy_memory_queue(_vault(tmp_path), state_root)

    assert isinstance(queue, MemoryQueue)
    assert not (state_root / "run" / "queue-migrated-v2").exists()


def test_doctor_repair_neither_imports_nor_marks_the_json_queue(tmp_path: Path, monkeypatch) -> None:
    root, state_root, home = _build_root(tmp_path)
    _json_queue(state_root, "legacy-1.json", "legacy-2.processing")
    monkeypatch.setattr(doctor, "_rebuild_index", lambda root, state: None)

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    check = _check(report, "queue")
    assert [item["action"] for item in report["repaired"] if item["action"] in {"migrate_queue", "recover_stale_lease"}] == []
    assert not (state_root / "run" / "queue-migrated-v2").exists()
    assert sorted(path.name for path in (state_root / "run" / "queue").iterdir()) == ["legacy-1.json", "legacy-2.processing"]
    assert (check["status"], check["details"]["legacy_retained"]) == ("error", 2)
    assert any("legacy_json_queue_unsupported" in error for error in check["details"]["repair_errors"])


@pytest.mark.parametrize("installer", ["install.sh", "install.ps1"])
def test_the_installers_no_longer_create_the_json_queue_directory(installer: str) -> None:
    text = (REPO / installer).read_text(encoding="utf-8")
    assert "run/queue\"" not in text and "run\\queue\"" not in text
