"""The vault log is written to a private file; the tracked `knowledge/log.md` stays the template.

The tracked log collected every agent's prose and every compile line, one `git commit -a`
from being published. Research: `docs/research/2026-09-14-the-vault-log-is-private.md`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import vault_log  # noqa: E402


def test_the_private_log_is_ignored_by_git():
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", vault_log.LOG_RELATIVE], cwd=ROOT, check=False
    )

    assert ignored.returncode == 0


def test_the_tracked_log_is_only_the_template():
    shipped = (ROOT / "knowledge" / "log.md").read_text(encoding="utf-8")

    assert [line for line in shipped.splitlines() if line.startswith("- ")] == []


def test_every_runtime_writer_and_reader_names_the_private_log():
    import compile_memory
    import query_memory
    import session_start_context

    paths = {compile_memory.LOG.name, query_memory.LOG.name, session_start_context.MEMORY_LOG.name}

    assert paths == {vault_log.LOG_NAME}
