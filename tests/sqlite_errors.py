"""SQLite errors shaped as the driver raises them: the code decides, not the text."""

import sqlite3


def busy_database_error() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


def locked_table_error() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database table is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_LOCKED
    return error
