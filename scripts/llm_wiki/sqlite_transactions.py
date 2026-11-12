"""Explicit SQLite transaction control on a caller-owned connection."""
from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Iterator


@contextlib.contextmanager
def begin_immediate(
    connection: sqlite3.Connection,
    *,
    before_commit: Callable[[], None] | None = None,
) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        if before_commit is not None:
            before_commit()
        connection.commit()
    except BaseException as original:
        try:
            connection.rollback()
        except BaseException as rollback_error:
            original.__context__ = rollback_error
        raise

# Keep historical function pickle lookup and introspection paths.
begin_immediate.__module__ = 'reliable_memory'
begin_immediate.__wrapped__.__module__ = 'reliable_memory'
