"""Read-only access to a queue-v3 database and the compile artifacts it retains."""
from __future__ import annotations

from functools import partial
from pathlib import Path

import compile_progress
from llm_wiki import sqlite_io
from llm_wiki.queue_artifacts import digest_stable_owner_file
from llm_wiki.queue_contracts import QueueOperationError
from llm_wiki.queue_database_contract import checked_queue_connection
from llm_wiki.reliability_defaults import DEFAULTS


def open_queue_v3_readonly(path: Path, state_root: Path, *, deadline: float | None = None, expected_contract=None):
    """Read a supported queue version; adoption still binds the exact active one."""
    return checked_queue_connection(sqlite_io.open_readonly_operational_db(
        path, state_root, max_bytes=None, owner_only=True,
        busy_ms=DEFAULTS.queue_busy_ms, deadline=deadline,
    ), expected_contract)


def verify_retained_artifact(state_root: Path, event, *, deadline=None) -> int:
    """Verify retained bytes in bounded memory; return the physical size."""
    relative, expected = compile_progress.artifact_identity(event)
    try:
        digest, size = digest_stable_owner_file(state_root / relative, deadline=deadline)
    except TimeoutError:
        raise
    except (OSError, ValueError, QueueOperationError) as error:
        raise compile_progress.ProgressCorrupt('compile artifact is unavailable') from error
    if digest != expected:
        raise compile_progress.ProgressCorrupt('compile artifact digest mismatch')
    return size


def audit_compile_progress(database, state_root: Path, *, deadline=None):
    """Share intermediate-artifact certification with doctor and staged backup."""
    verify = partial(verify_retained_artifact, Path(state_root), deadline=deadline)
    return compile_progress.audit_events(database, verify, deadline=deadline)
