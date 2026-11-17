"""One table decoder shared by owner and MCP integration tests."""

def _table(answer: dict, key: str) -> list[dict]:
    """Rows as objects, whichever shape the answer used.

    Since 2026-09-12 a table that measures cheaper is emitted as `<key>_cols`
    plus one array per row, so a test that reads a field by name asks for the
    rows through here and stays true under either shape.
    """
    rows = answer[key]
    cols = answer.get(f"{key}_cols")
    if cols is None:
        return rows
    return [dict(zip(cols, row)) for row in rows]
