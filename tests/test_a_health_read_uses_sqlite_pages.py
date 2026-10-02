"""A health SQLite query does not allocate the whole database file."""
from pathlib import Path

import doctor
import pytest

from tests.test_reliability_v3_adoption import _vault, build_adopted_reliability_v3


@pytest.mark.parametrize("name", ["queue-v3.sqlite3", "markdown-transactions-v3.sqlite3"])
def test_health_reads_a_valid_database_past_the_old_whole_file_ceiling(tmp_path: Path, name: str):
    root, state = _vault(tmp_path)
    build_adopted_reliability_v3(root, state)
    path = state / "run" / name
    with path.open("r+b") as handle:
        handle.truncate(256 * 1024 * 1024 + 4096)
    with doctor._readonly_database(path, state) as database:
        assert database.execute("PRAGMA query_only").fetchone()[0] == 1
        assert database.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone()
