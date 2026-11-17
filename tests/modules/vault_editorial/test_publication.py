"""The actual repository keeps private log content out of publication."""
import subprocess
from pathlib import Path

import vault_log

ROOT = Path(__file__).resolve().parents[3]


def test_the_private_log_is_ignored_by_git():
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", vault_log.LOG_RELATIVE], cwd=ROOT, check=False
    )

    assert ignored.returncode == 0


def test_the_tracked_log_is_only_the_template():
    shipped = (ROOT / "knowledge" / "log.md").read_text(encoding="utf-8")

    assert [line for line in shipped.splitlines() if line.startswith("- ")] == []
