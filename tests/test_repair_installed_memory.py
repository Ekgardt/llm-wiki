from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import markdown_transaction
import memory_queue
import pytest
import repair_installed_memory

SCRIPT = Path(__file__).parents[1] / "scripts" / "repair_installed_memory.py"
ADAPTER = SCRIPT.with_name("integration_adapter.py")


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    root.mkdir()
    (root / "scripts").mkdir()
    (root / "scripts/integration_adapter.py").write_bytes(ADAPTER.read_bytes())
    return root, state_root


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("LLM_WIKI_ROOT", None)
    environment.pop("LLM_WIKI_STATE_ROOT", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_default_mode_is_read_only_check(tmp_path: Path) -> None:
    root, state_root = _vault(tmp_path)

    completed = _run(
        "--root",
        str(root),
        "--state-root",
        str(state_root),
        "--json",
    )

    report = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert report["mode"] == "check"
    assert report["overall_status"] == "degraded"
    assert report["details"]["adoption_state"] == "fresh"
    assert not state_root.exists()


def test_check_and_apply_are_mutually_exclusive(tmp_path: Path) -> None:
    root, state_root = _vault(tmp_path)

    completed = _run(
        "--check",
        "--apply",
        "--root",
        str(root),
        "--state-root",
        str(state_root),
    )

    assert completed.returncode == 2
    assert "not allowed with argument" in completed.stderr
    assert not state_root.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ("--adopt-ownership-v3",),
        ("--apply", "--adopt-ownership-v3"),
        ("--apply", "--confirm-all-agents-stopped"),
    ],
)
def test_mutation_flags_require_apply_and_the_complete_offline_gate(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    root, state_root = _vault(tmp_path)

    completed = _run(
        *arguments,
        "--root",
        str(root),
        "--state-root",
        str(state_root),
    )

    assert completed.returncode == 2
    assert not state_root.exists()


def test_complete_offline_gate_initializes_fresh_pair(
    tmp_path: Path,
) -> None:
    root, state_root = _vault(tmp_path)

    completed = _run(
        "--apply",
        "--adopt-ownership-v3",
        "--confirm-all-agents-stopped",
        "--root",
        str(root),
        "--state-root",
        str(state_root),
        "--json",
    )

    report = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert report == {
        "actions": [{"code": "reliability_v3_adopted"}],
        "blockers": [],
        "details": {"adoption_state": "adopted"},
        "mode": "apply",
        "overall_status": "ok",
    }
    run = state_root / "run"
    assert not (run / "queue-v2-retired.sqlite3").exists()
    assert not (run / "markdown-transactions-v2-retired.sqlite3").exists()
    assert json.loads((run / "queue.sqlite3").read_bytes())["source_state"] == "fresh"
    assert json.loads((run / "markdown-transactions.sqlite3").read_bytes())[
        "source_state"
    ] == "fresh"
    assert memory_queue.validate_queue_v3_database(
        run / "queue-v3.sqlite3", state_root=state_root
    )["integrity_check"] == "ok"
    assert markdown_transaction.validate_coordinator_v3_database(
        run / "markdown-transactions-v3.sqlite3", state_root=state_root
    )["integrity_check"] == "ok"


def test_cli_redacts_backend_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, state_root = _vault(tmp_path)
    secret = str(tmp_path / "private" / "vault")

    def fail(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError(secret)

    monkeypatch.setattr(repair_installed_memory, "inspect_installed_vault", fail)

    result = repair_installed_memory.main(
        ["--root", str(root), "--state-root", str(state_root), "--json"]
    )
    output = capsys.readouterr().out
    report = json.loads(output)

    assert result == 2
    assert secret not in output
    assert report == {
        "actions": [],
        "blockers": [{"code": "repair_backend_error"}],
        "details": {},
        "mode": "check",
        "overall_status": "error",
    }


def test_help_documents_non_destructive_offline_contract() -> None:
    completed = _run("--help")
    normalized = " ".join(completed.stdout.split())

    assert completed.returncode == 0
    for phrase in (
        "read-only validation; this is the default",
        "permit the selected resumable repair",
        "perform the offline v3 adoption",
        "never removes run/, knowledge, legacy caches, or compatibility markers",
    ):
        assert phrase in normalized
