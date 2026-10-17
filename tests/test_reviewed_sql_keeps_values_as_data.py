"""SQL-looking identifiers must select only their literal matching rows."""
import sqlite3
from contextlib import closing

import blackboard
import code_hints
import fact_keys
import pytest
import retrieval_telemetry

from tests.test_retrieval_telemetry import _event


@pytest.fixture(params=["x' OR 1=1 --", "x'); DROP TABLE symbol; --", 'quoted"value'])
def identifier(request):
    return request.param


def test_blackboard_resource_and_project_are_literal_values(identifier):
    with closing(sqlite3.connect(":memory:")) as database:
        database.row_factory = sqlite3.Row
        database.execute("CREATE TABLE blackboard_claims(project TEXT, resource TEXT)")
        rows = [(identifier, identifier), (identifier, "other"), ("other", identifier)]
        database.executemany("INSERT INTO blackboard_claims VALUES (?, ?)", rows)
        found = blackboard._busy_claim_rows(database, identifier, (identifier,))
        assert [tuple(row) for row in found] == [rows[0]]
        assert database.execute("SELECT COUNT(*) FROM blackboard_claims").fetchone()[0] == 3


def test_symbol_name_is_a_literal_value(identifier):
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute("CREATE TABLE symbol(name TEXT, qualified_name TEXT, kind TEXT, path TEXT, line INTEGER, in_degree INTEGER, out_degree INTEGER)")
        row = (identifier, identifier, "function", "example.py", 1, 0, 0)
        database.executemany("INSERT INTO symbol VALUES (?, ?, ?, ?, ?, ?, ?)", [row, ("other", "other", *row[2:])])
        total, rows = code_hints._lookup_rows(database, identifier)
        assert (total, rows) == (1, [row[1:]])
        assert database.execute("SELECT COUNT(*) FROM symbol").fetchone()[0] == 2


def test_fact_key_span_is_a_literal_value(identifier):
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute("CREATE TABLE keys(id INTEGER, span_sha256 TEXT, key TEXT)")
        database.executemany("INSERT INTO keys VALUES (?, ?, ?)", [(1, identifier, "matched"), (2, "other", "unrelated")])
        assert fact_keys._span_rows(database, [identifier]) == [(identifier, "matched")]
        assert database.execute("SELECT COUNT(*) FROM keys").fetchone()[0] == 2


def test_telemetry_candidate_is_a_literal_value(tmp_path, identifier):
    database = tmp_path / "telemetry.sqlite3"
    events = [_event(candidate_id=identifier), _event(candidate_id="other")]
    assert retrieval_telemetry.record_events(events, db_path=database) == 2
    found = retrieval_telemetry.read_events(candidate_id=identifier, db_path=database)
    assert [event.candidate_id for event in found] == [identifier]
    assert len(retrieval_telemetry.read_events(db_path=database)) == 2
