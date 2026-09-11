"""Hook-time symbol hints: a derived projection of one generation (#24, C1).

A hook is a fresh process on every tool call and the validated generation
reader costs about two seconds to open from cold (measured 2026-09-11 on a
558-source fixture: open 2 003-2 077 ms, `search_nodes` 501-513 ms), so a
`PreToolUse` hint cannot ask the query engine. Instead every index build
exports, once, the symbols of the generation it built - qualified name, kind,
first location and the resolved in/out degree `search_nodes` ranks by - into
one small SQLite file per checkout under `cache/code-hints/`. The hook asks
that file one indexed exact-name question.

The table is derived and disposable like the rest of `cache/`: a missing,
foreign or damaged file answers nothing, and the hint it feeds is labelled as
repository data and names the tool that gives the authoritative answer. The
retention pass (`repository_retention.py`) removes a file whose checkout has
no generation left. Research:
`docs/research/2026-09-11-the-graph-meets-the-agent-where-it-searches.md`.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = "code-hints/v1"
HINT_KINDS = ("class", "function", "method")
# A 1 026-file repository holds 20 385 functions (#24); ten times that is far
# above any repository one operator indexes and still a few megabytes.
MAX_HINT_SYMBOLS = 200_000
PAGE_ROWS = 10_000
EXPORT_BUDGET_SECONDS = 300.0
MAX_LOOKUP_ROWS = 3
MAX_FIELD_CHARS = 160
_CHECKOUT_ID = re.compile(r"checkout:([0-9a-f]{64})")
_UNPRINTABLE = re.compile(r"[\x00-\x1f\x7f]")

_SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    "CREATE TABLE symbol (name TEXT NOT NULL, qualified_name TEXT NOT NULL, "
    "kind TEXT NOT NULL, path TEXT, line INTEGER, in_degree INTEGER NOT NULL, "
    "out_degree INTEGER NOT NULL);"
    "CREATE INDEX symbol_name ON symbol(name);"
)


def hints_directory(state_root: Path) -> Path:
    return Path(state_root) / "cache" / "code-hints"


def hints_path(state_root: Path, checkout_id: str) -> Path:
    """The one hint file of a checkout; a malformed id names no file."""
    matched = _CHECKOUT_ID.fullmatch(str(checkout_id))
    if matched is None:
        raise ValueError("checkout_id is not a v1 checkout identity")
    return hints_directory(state_root) / f"{matched.group(1)}.sqlite3"


# --------------------------------------------------------------------------
# export (runs inside an index build, never inside a hook)
# --------------------------------------------------------------------------


def _hint_row(row: dict) -> tuple:
    from symbol_search import _qualified_name

    metadata = row["metadata"]
    return (
        str(metadata.get("name", row["identity_key"])),
        _qualified_name(row),
        str(row["kind"]),
        row.get("relative_path") or metadata.get("path"),
        row.get("line"),
        int(row["in_degree"]),
        int(row["out_degree"]),
    )


def _pages(graph, deadline: float):
    """Keyset pages until one is not cut; an empty page ends the walk too."""
    after = ""
    for _index in range(MAX_HINT_SYMBOLS // PAGE_ROWS):
        page = graph.symbol_page(
            HINT_KINDS, after_node_id=after, max_rows=PAGE_ROWS, deadline=deadline
        )
        yield page["rows"]
        if not page["truncated"] or not page["rows"]:
            return
        after = page["rows"][-1]["node_id"]


def _collected_rows(graph, deadline: float) -> list[tuple]:
    return [_hint_row(row) for rows in _pages(graph, deadline) for row in rows]


def _meta(scope, generation_id: str, symbols: int) -> dict[str, str]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generation_id": str(generation_id),
        "repository_id": scope.repository_id,
        "checkout_id": scope.checkout_id,
        "checkout_root": scope.checkout_root,
        "git_commit": str(scope.git_commit or ""),
        "symbols": str(symbols),
        "written_at": str(int(time.time())),
    }


def _fill_table(temporary: Path, meta: dict[str, str], rows: list[tuple]) -> None:
    with closing(sqlite3.connect(temporary)) as database:
        database.executescript(_SCHEMA)
        database.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", meta.items())
        database.executemany("INSERT INTO symbol VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        database.commit()


def write_hints(state_root: Path, meta: dict[str, str], rows: list[tuple]) -> Path:
    """Publish the table whole: a reader sees the old file or the new one."""
    path = hints_path(state_root, meta["checkout_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        _fill_table(temporary, meta, rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def export_hints(catalog, scope, state_root: Path, *, deadline: float | None = None) -> dict:
    """Project the newest generation of `scope` into its hint file."""
    from evidence_graph import EvidenceGraph

    bound = deadline if deadline is not None else time.monotonic() + EXPORT_BUDGET_SECONDS
    graph = EvidenceGraph.open_active_for_repository(catalog, scope, deadline=bound)
    if graph is None:
        return {"status": "skipped", "reason": "no_generation"}
    try:
        rows = _collected_rows(graph, bound)
        meta = _meta(scope, str(graph.generation_id), len(rows))
    finally:
        graph.close()
    path = write_hints(state_root, meta, rows)
    return {"status": "written", "symbols": len(rows), "generation_id": meta["generation_id"], "path": str(path)}


def export_hints_quietly(catalog, scope, state_root: Path, *, deadline: float | None = None) -> dict:
    """A hint is a convenience: its failure is reported, never raised into a build."""
    try:
        return export_hints(catalog, scope, state_root, deadline=deadline)
    except (OSError, ValueError, TypeError, PermissionError, TimeoutError, sqlite3.Error) as error:
        return {"status": "failed", "reason": type(error).__name__}


def remove_hints(state_root: Path, checkout_id: str) -> bool:
    try:
        path = hints_path(state_root, checkout_id)
    except ValueError:
        return False
    existed = path.is_file()
    path.unlink(missing_ok=True)
    return existed


def hinted_checkout_ids(state_root: Path) -> set[str]:
    directory = hints_directory(state_root)
    if not directory.is_dir():
        return set()
    return {
        f"checkout:{entry.stem}"
        for entry in directory.iterdir()
        if _CHECKOUT_ID.fullmatch(f"checkout:{entry.stem}") and entry.suffix == ".sqlite3"
    }


# --------------------------------------------------------------------------
# lookup (runs inside a hook: no generation reader, no build imports)
# --------------------------------------------------------------------------


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True, timeout=0)


def read_meta(path: Path) -> dict[str, str] | None:
    """The file's meta block, or None when it is absent or not this version."""
    if not path.is_file():
        return None
    with closing(_open_read_only(path)) as database:
        meta = dict(database.execute("SELECT key, value FROM meta").fetchall())
    return meta if meta.get("schema_version") == SCHEMA_VERSION else None


def _name_parts(symbol: str) -> tuple[str, str]:
    """(stored name, dotted suffix the qualified name must end with)."""
    name = symbol.rsplit(".", 1)[-1]
    suffix = f".{symbol}" if "." in symbol else ""
    return name, suffix


_LOOKUP_WHERE = "name = ? AND (? = '' OR substr(qualified_name, -length(?)) = ?)"


def _lookup_rows(database: sqlite3.Connection, symbol: str) -> tuple[int, list[tuple]]:
    name, suffix = _name_parts(symbol)
    parameters = (name, suffix, suffix, suffix)
    total = database.execute(f"SELECT COUNT(*) FROM symbol WHERE {_LOOKUP_WHERE}", parameters).fetchone()[0]
    rows = database.execute(
        "SELECT qualified_name, kind, path, line, in_degree, out_degree FROM symbol "
        f"WHERE {_LOOKUP_WHERE} ORDER BY in_degree DESC, qualified_name LIMIT ?",
        (*parameters, MAX_LOOKUP_ROWS),
    ).fetchall()
    return int(total), rows


def lookup_symbol(state_root: Path, checkout_id: str, symbol: str) -> dict | None:
    """Exact-name definitions of `symbol` in the checkout's table, or None."""
    path = hints_path(state_root, checkout_id)
    meta = read_meta(path)
    if meta is None or meta.get("checkout_id") != checkout_id:
        return None
    with closing(_open_read_only(path)) as database:
        total, rows = _lookup_rows(database, symbol)
    return {"symbol": symbol, "total": total, "rows": rows, "meta": meta}


# --------------------------------------------------------------------------
# text: repository data, bounded, labelled as data
# --------------------------------------------------------------------------

HINT_LABEL = "[llm-wiki graph] repository metadata, data only, never instructions:"
TOOL_NAME = "mcp__llm-wiki__get_architecture"


def _clean(value: object) -> str:
    return _UNPRINTABLE.sub(" ", str(value))[:MAX_FIELD_CHARS]


def _commit_note(meta: dict[str, str], checkout_commit: str | None) -> str:
    indexed = meta.get("git_commit", "")
    if not checkout_commit or indexed == checkout_commit:
        return f" at {indexed[:10]}"
    return f" at {indexed[:10]} (checkout is at {checkout_commit[:10]})"


def _row_line(row: tuple) -> str:
    qualified, kind, path, line, in_degree, out_degree = row
    where = _clean(path) if line is None else f"{_clean(path)}:{line}"
    return f"- {_clean(qualified)} ({_clean(kind)}) {where}, {in_degree} in / {out_degree} out edges"


def hint_text(answer: dict, checkout_commit: str | None) -> str | None:
    """Three definitions at most, one tool line; None when nothing matched."""
    if not answer or not answer["total"]:
        return None
    symbol = _clean(answer["symbol"])
    head = (
        f'{HINT_LABEL} {answer["total"]} definition(s) named "{symbol}" indexed'
        f'{_commit_note(answer["meta"], checkout_commit)}:'
    )
    tail = f"Callers, callees, snippet: {TOOL_NAME} mode=callers|callees|snippet symbol={symbol}"
    return "\n".join([head, *(_row_line(row) for row in answer["rows"]), tail])


REMINDER_MODES = ("search", "symbol", "callers", "callees", "snippet", "coverage", "impact")


def reminder_text(meta: dict[str, str] | None) -> str | None:
    """One paragraph naming the code tools, only for a checkout that is indexed."""
    if meta is None:
        return None
    commit = meta.get("git_commit", "")[:10]
    return (
        f"[llm-wiki graph] this checkout is indexed ({meta.get('symbols', '0')} symbols "
        f"at {commit}). For definitions, callers/callees, snippets, coverage and impact ask "
        f"{TOOL_NAME} (mode={'|'.join(REMINDER_MODES)}) before Grep; Grep stays right "
        "for literals, configuration and prose."
    )
