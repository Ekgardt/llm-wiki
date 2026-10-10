"""Dense admission retains its true weight without evaluating a discarded lexical one."""
import sqlite3
from unittest.mock import Mock

import search_memory

from tests.test_dense_admission_trust_weight import _COLUMNS, _row, _scored


def test_each_eligible_span_is_weighed_once_with_exact_existing_results(monkeypatch):
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(f"CREATE TABLE chunks ({_COLUMNS})")
        connection.executemany(
            f"INSERT INTO chunks VALUES ({','.join('?' * 18)})",
            (_row(0, "docs/research/topic.md", "doc"), _row(1, "knowledge/notes/decision.md", "decision")),
        )
        connection.execute("UPDATE chunks SET content=? WHERE chunk_order=0", ("## Related\n- [[one]]\n- [[two]]\n",))
        expected = _scored(connection, [0.9, 0.8])
        weigh = Mock(wraps=search_memory._chunk_weight)
        monkeypatch.setattr(search_memory, "_chunk_weight", weigh)
        actual = _scored(connection, [0.9, 0.8])
    assert actual == expected
    assert weigh.call_count == len(actual)
    assert [row["path"] for row in actual] == ["knowledge/notes/decision.md", "docs/research/topic.md"]
