"""A vault that went through the real V3 adoption, for tests of the adopted path."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from installed_memory_repair import repair_installed_vault  # noqa: E402


def adopt(tmp_path: Path) -> tuple[Path, Path]:
    """Build a vault and a state root and run the real adoption command on them."""
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/integration_adapter.py").write_bytes(
        (SCRIPTS_DIR / "integration_adapter.py").read_bytes()
    )
    report = repair_installed_vault(
        root=root,
        state_root=state_root,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )
    if report["overall_status"] != "ok":
        raise AssertionError(report)
    return root, state_root


def tamper_payload(state_root: Path, task_id: str) -> None:
    """Change one task's stored payload past the API, as bit rot or an editor would."""
    with sqlite3.connect(state_root / "run/queue-v3.sqlite3") as connection:
        connection.execute(
            "UPDATE tasks SET payload_blob=? WHERE id=?",
            (b'{"tampered":true}', task_id),
        )


def task_state(state_root: Path, task_id: str) -> tuple[str, str | None]:
    """The stored state and error code of one task."""
    with sqlite3.connect(state_root / "run/queue-v3.sqlite3") as connection:
        row = connection.execute(
            "SELECT state, error_code FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    return (str(row[0]), row[1])
