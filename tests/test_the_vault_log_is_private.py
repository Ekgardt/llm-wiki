"""The vault log is written to a private file; the tracked `knowledge/log.md` stays the template.

The tracked log collected every agent's prose and every compile line, one `git commit -a`
from being published. Research: `docs/research/2026-09-14-the-vault-log-is-private.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import vault_log  # noqa: E402


def test_every_runtime_writer_and_reader_names_the_private_log():
    import compile_memory
    import query_memory
    import session_start_context

    paths = {compile_memory.LOG.name, query_memory.LOG.name, session_start_context.MEMORY_LOG.name}

    assert paths == {vault_log.LOG_NAME}
