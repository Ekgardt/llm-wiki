"""The public repair command preserves the offline migration's safety gates."""
from __future__ import annotations

import json

import compile_progress_migration as migration
import memory_queue
import private_vault_backup as backup
import pytest

from tests.adopted_vault import adopt
from tests.test_repair_installed_memory import _run


def _arguments(root, state, image):
    return (
        "--apply", "--upgrade-compile-progress", "--backup-image", str(image),
        "--confirm-all-agents-stopped", "--root", str(root),
        "--state-root", str(state), "--json",
    )


@pytest.mark.parametrize("arguments", [
    ("--upgrade-compile-progress",),
    ("--apply", "--upgrade-compile-progress"),
    ("--apply", "--upgrade-compile-progress", "--confirm-all-agents-stopped"),
    ("--apply", "--upgrade-compile-progress", "--backup-image", "unused"),
    ("--backup-image", "unused"),
    ("--apply", "--adopt-ownership-v3", "--confirm-all-agents-stopped",
     "--backup-image", "unused"),
    ("--apply", "--adopt-ownership-v3", "--upgrade-compile-progress",
     "--confirm-all-agents-stopped", "--backup-image", "unused"),
])
def test_incomplete_or_ambiguous_operation_cannot_touch_state(tmp_path, arguments):
    root, state = tmp_path / "vault", tmp_path / "state"
    result = _run(*arguments, "--root", str(root), "--state-root", str(state))
    assert result.returncode == 2
    assert not root.exists()
    assert not state.exists()


def test_cli_upgrades_and_preserves_new_work_on_repeat(tmp_path):
    root, state = adopt(tmp_path)
    image = tmp_path / "preimage"
    image.mkdir(mode=0o700)
    result = _run(*_arguments(root, state, image))
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["details"]["compile_progress"] == "upgraded"
    assert report["actions"] == [{"code": "compile_progress_upgraded"}]
    assert backup.validate_backup_image(image)["database_count"] == 2
    queue = memory_queue.active_memory_queue(root, state)
    task = queue.enqueue("compile", 1, {"after": "migration"})
    repeated = _run(*_arguments(root, state, image))
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert json.loads(repeated.stdout) == report
    assert queue.get(task).payload == {"after": "migration"}


def test_cli_confirmation_does_not_override_live_owner(tmp_path):
    root, state = adopt(tmp_path)
    image = tmp_path / "preimage"
    image.mkdir(mode=0o700)
    before = (state / "run/reliability-v3-adopted.json").read_bytes()
    queue = memory_queue.active_memory_queue(root, state)
    with queue.queue_owner(role="queue-worker", scope="repair-cli-live-owner"):
        result = _run(*_arguments(root, state, image))
    assert result.returncode == 2
    assert "quiescence" in json.loads(result.stdout)["details"]["error"]
    assert (state / "run/reliability-v3-adopted.json").read_bytes() == before
    assert list(image.iterdir()) == []


def _interrupt_after_intent(point):
    if point == "after_intent":
        raise RuntimeError("injected interruption")


def test_cli_resumes_bound_preimage_and_refuses_a_different_one(tmp_path):
    root, state = adopt(tmp_path)
    image = tmp_path / "preimage"
    image.mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match="injected"):
        migration.upgrade_offline(
            root=root, state_root=state, backup_image=image,
            confirm_all_agents_stopped=True, killpoint=_interrupt_after_intent,
        )
    wrong = tmp_path / "wrong-preimage"
    wrong.mkdir(mode=0o700)
    failed = _run(*_arguments(root, state, wrong))
    assert failed.returncode == 2
    assert json.loads(failed.stdout)["overall_status"] == "error"
    assert str(tmp_path) not in failed.stdout
    resumed = _run(*_arguments(root, state, image))
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert json.loads(resumed.stdout)["details"]["compile_progress"] == "upgraded"
    checked = _run("--check", "--root", str(root), "--state-root", str(state), "--json")
    assert checked.returncode == 0, checked.stdout + checked.stderr
